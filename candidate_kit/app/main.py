"""Employee attendance and analytics API."""

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=2000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMAIL_PATTERN = re.compile(r"^(?!.*[\\/])[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_department_joined",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_department_code",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_employee_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_employee_punch_in",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def _date(value: str) -> date:
    if not DATE_PATTERN.fullmatch(value):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


def _request_date(value: str) -> date:
    try:
        return _date(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _truncate_second(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def _epoch_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timestamp must be an integer epoch-millisecond value")
    if value < EPOCH_MIN or value > EPOCH_MAX:
        raise ValueError("timestamp is outside the supported range")
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("timestamp is outside the supported range") from exc


def _to_epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _round_half_up(value: Decimal, places: int) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    start = time.fromisoformat(shift_start)
    end = time.fromisoformat(shift_end)
    day = local.date()
    if end <= start and local.time().replace(tzinfo=None) < end:
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: date | None = None) -> int:
    punch_in = _truncate_second(punch_in).astimezone(IST)
    if attendance_day is None:
        attendance_day = punch_in.date()
    h, m = map(int, shift_start.split(":"))
    shift_start_at = datetime.combine(attendance_day, time(h, m), tzinfo=IST)
    elapsed = int((punch_in - shift_start_at).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((_truncate_second(punch_out) - _truncate_second(punch_in)).total_seconds())
    return _round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    attendance_day: date,
    shift_start: str,
) -> int:
    end_day = attendance_day
    if time.fromisoformat(shift_end) <= time.fromisoformat(shift_start):
        end_day += timedelta(days=1)
    h, m = map(int, shift_end.split(":"))
    shift_end_at = datetime.combine(end_day, time(h, m), tzinfo=IST)
    elapsed = int((_truncate_second(punch_out).astimezone(IST) - shift_end_at).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= 30 else 0


def _serialize_attendance(doc: dict) -> dict:
    result = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _to_epoch(doc.get("punch_in")),
        "punch_out": _to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    for entry in doc.get("history", []):
        changes = {}
        for field, values in entry.get("changes", {}).items():
            changes[field] = {
                "from": _to_epoch(values.get("from")) if field in ("punch_in", "punch_out") else values.get("from"),
                "to": _to_epoch(values.get("to")) if field in ("punch_in", "punch_out") else values.get("to"),
            }
        result["history"].append(
            {
                "at": _to_epoch(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return result


def _serialize_employee(doc: dict) -> dict:
    result = {key: doc[key] for key in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}
    result["created_at"] = _to_epoch(doc["created_at"])
    return result


def _month_bounds(month: str) -> tuple[str, str, date, date]:
    if not MONTH_PATTERN.fullmatch(month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month_num = map(int, month.split("-"))
    try:
        start = date(year, month_num, 1)
        end = date(year, month_num, calendar.monthrange(year, month_num)[1])
    except ValueError as exc:
        raise HTTPException(422, "month is outside the supported calendar range") from exc
    return start.isoformat(), end.isoformat(), start, end


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("email must be a valid email address without slashes or backslashes")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value: str) -> str:
        if not TIME_PATTERN.fullmatch(value):
            raise ValueError("shift time must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        _date(value)
        return value

    @model_validator(mode="after")
    def different_shift_bounds(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def timestamp_cannot_be_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)

    @model_validator(mode="after")
    def timestamp_cannot_be_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: StrictInt | None = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def punch_times_cannot_be_null(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        if "punch_in" in self.model_fields_set and self.punch_in is None:
            raise ValueError("punch_in cannot be null")
        if "punch_out" in self.model_fields_set and self.punch_out is None:
            raise ValueError("punch_out cannot be null")
        return self


def _monthly_stats_pipeline(
    emp_code: str,
    start: str,
    end: str,
    month_start: date,
    month_end: date,
) -> list[dict]:
    days_in_month = (month_end - month_start).days + 1
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start]},
                    {"$lte": ["$date", end]},
                ]}}},
                {"$set": {"_is_weekday": {"$in": [
                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    [2, 3, 4, 5, 6],
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": ["$_is_weekday", {"$in": ["$status", list(PRESENCE_STATUSES)]}]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0,
                    ]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }},
            ],
            "as": "_monthly",
        }},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "working_days": {"$size": {"$filter": {
                "input": {"$map": {
                    "input": {"$range": [0, days_in_month]},
                    "as": "offset",
                    "in": {"$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {"$dateAdd": {
                            "startDate": {"$dateFromString": {"dateString": month_start.isoformat()}},
                            "unit": "day",
                            "amount": "$$offset",
                        }},
                    }},
                }},
                "as": "day",
                "cond": {"$and": [
                    {"$gte": ["$$day", "$joined_on"]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$day"}}}, [2, 3, 4, 5, 6]]},
                ]},
            }}},
            "_stats": {"$arrayElemAt": ["$_monthly", 0]},
        }},
        {"$project": {
            "emp_code": 1,
            "working_days": 1,
            "present_days": {"$ifNull": ["$_stats.present_days", 0]},
            "leave_days": {"$ifNull": ["$_stats.leave_days", 0]},
            "late_count": {"$ifNull": ["$_stats.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$_stats.total_late_minutes", 0]},
            "total_overtime_minutes": {"$ifNull": ["$_stats.total_overtime_minutes", 0]},
        }},
    ]


def _department_summary_pipeline(start: str, end: str, department: str | None) -> list[dict]:
    employee_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", start]},
                        {"$lte": ["$date", end]},
                    ]}}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "_has_log": {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                "_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start]}}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employee_codes": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": {"$cond": [
                    {"$and": ["$_has_log", "$_weekday", {"$in": ["$logs.status", list(PRESENCE_STATUSES)]}]},
                    {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                    0,
                ]}},
                "avg_work_hours": {"$avg": {"$cond": [
                    {"$and": ["$_has_log", {"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                    "$logs.work_hours",
                    None,
                ]}},
                "late_count": {"$sum": {"$cond": [{"$and": ["$_has_log", {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$cond": ["$_has_log", {"$ifNull": ["$logs.late_minutes", 0]}, 0]}},
                "leave_count": {"$sum": {"$cond": [{"$and": ["$_has_log", {"$eq": ["$logs.status", "LEAVE"]}]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$and": ["$_has_log", {"$eq": ["$logs.status", "ON_DUTY"]}]}, 1, 0]}},
            }
        },
        {"$set": {"headcount": {"$size": "$employee_codes"}}},
        {"$unset": "employee_codes"},
        {"$sort": {"_id": 1}},
    ]


def _leaderboard_pipeline(start: str, end: str, department: str | None, limit: int) -> list[dict]:
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        *([{"$match": {"employee.department": department}}] if department is not None else []),
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]


def _trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    count_days = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {
            "_id": 0,
            "employees": 1,
            "dates": {"$map": {
                "input": {"$range": [0, count_days]},
                "as": "offset",
                "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start.isoformat()}}, "unit": "day", "amount": "$$offset"}},
            }},
        }},
        {"$unwind": "$dates"},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$dates"}, [2, 3, 4, 5, 6]]},
            "headcount": {"$size": {"$filter": {
                "input": "$employees",
                "as": "employee",
                "cond": {"$lte": ["$$employee.joined_on", {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates"}}]},
            }}},
            "_employee_codes": {"$map": {"input": "$employees", "as": "employee", "in": "$$employee.emp_code"}},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date", "codes": "$_employee_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
            "as": "_logs",
        }},
        {"$set": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$_logs.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$_logs.late_count", 0]}, 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [
                {"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}},
                10000,
            ]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"dates": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$eq": ["$moving_avg_7d", None]},
            None,
            {"$divide": [
                {"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}},
                10000,
            ]},
        ]}}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]


@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = _truncate_second(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_serialize_employee(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = _truncate_second(_epoch_datetime(body.punched_at)) if body.punched_at is not None else _truncate_second(datetime.now(UTC))
    attendance_day = _attendance_day(punched_at, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], attendance_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return _serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = _truncate_second(_epoch_datetime(body.punched_at)) if body.punched_at is not None else _truncate_second(datetime.now(UTC))
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_at = _truncate_second(record["punch_in"])
    duration = (punched_at - punch_in_at).total_seconds()
    if duration <= 0 or duration > 24 * 60 * 60:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    day = _date(record["date"])
    work_hours = compute_work_hours(punch_in_at, punched_at)
    overtime = compute_overtime(punched_at, employee["shift_end"], day, employee["shift_start"])
    half_day = work_hours < 4.50
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_in": record["punch_in"], "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}]},
        {"$set": {"punch_out": punched_at, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return _serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        _request_date(date_from)
    if date_to is not None:
        _request_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_serialize_attendance(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = _request_date(date)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status or record["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")
        new_punch_in = new_punch_out = None
    else:
        new_punch_in = _truncate_second(_epoch_datetime(body.punch_in)) if body.punch_in is not None else record.get("punch_in")
        new_punch_out = _truncate_second(_epoch_datetime(body.punch_out)) if body.punch_out is not None else record.get("punch_out")
        if new_punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if _attendance_day(new_punch_in, employee["shift_start"], employee["shift_end"]) != day:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if new_punch_out is not None:
            seconds = (new_punch_out - new_punch_in).total_seconds()
            if seconds <= 0 or seconds > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if new_status in ("ABSENT", "LEAVE"):
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        late = compute_late_minutes(new_punch_in, employee["shift_start"], day)
        if new_punch_out is None:
            derived = {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
        else:
            hours = compute_work_hours(new_punch_in, new_punch_out)
            derived = {
                "work_hours": hours,
                "late_minutes": late,
                "overtime_minutes": compute_overtime(new_punch_out, employee["shift_end"], day, employee["shift_start"]),
                "half_day": hours < 4.50,
            }

    proposed = {"status": new_status, "punch_in": new_punch_in, "punch_out": new_punch_out, **derived}
    changes = {}
    for key, value in proposed.items():
        old = record.get(key)
        if key == "half_day":
            old = record.get(key, False)
        if old != value:
            changes[key] = {"from": old, "to": value}
    if not changes:
        raise HTTPException(422, "regularization must change at least one field")

    now = _truncate_second(datetime.now(UTC))
    history_entry = {"at": now, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    optimistic_filter = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    if "history" in record:
        optimistic_filter["history"] = record["history"]
    else:
        optimistic_filter["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        optimistic_filter,
        {"$set": proposed, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return _serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    start, end, month_start, month_end = _month_bounds(month)
    records = list(db.employees.aggregate(
        _monthly_stats_pipeline(emp_code, start, end, month_start, month_end),
    ))
    if not records:
        raise HTTPException(404, "employee not found")
    stats = records[0]
    working_days = stats["working_days"]
    present_days = stats["present_days"]
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": _round_half_up(Decimal(str(present_days)), 2),
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": None if working_days == 0 else _round_half_up(Decimal(str(present_days)) * 100 / working_days, 2),
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
):
    start, end, _, _ = _month_bounds(month)
    results = db.employees.aggregate(_department_summary_pipeline(start, end, department))
    items = []
    for row in results:
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": _round_half_up(Decimal(str(row.get("present_days", 0))), 2),
            "avg_work_hours": None if row.get("avg_work_hours") is None else _round_half_up(Decimal(str(row["avg_work_hours"])), 2),
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
):
    start, end, _, _ = _month_bounds(month)
    items = list(db.attendance_logs.aggregate(_leaderboard_pipeline(start, end, department, limit)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = _request_date(from_date)
    end = _request_date(to_date)
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(_trend_pipeline(department, start, end)))
    return {"department": department, "items": items}


def _explain(collection, pipeline: list[dict] | None = None, find_query: dict | None = None, sort: list | None = None, skip: int = 0, limit: int = 20) -> dict:
    if pipeline is not None:
        command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}
    else:
        command = {"find": collection.name, "filter": find_query or {}, "sort": dict(sort or []), "skip": skip, "limit": limit}
    result = db.command("explain", command, verbosity="executionStats")
    return json_util.loads(json_util.dumps(result))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from is not None:
            _request_date(date_from)
        if date_to is not None:
            _request_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        query: dict = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status is not None:
            query["status"] = status
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _explain(
            db.attendance_logs, find_query=query, sort=[("date", DESCENDING), ("emp_code", ASCENDING)],
            skip=(page - 1) * page_size, limit=page_size,
        )}
    if endpoint in ("employee_monthly", "department_summary", "late_leaderboard") and month is None:
        raise HTTPException(422, "month is required")
    if endpoint == "employee_monthly":
        if emp_code is None:
            raise HTTPException(422, "emp_code is required")
        start, end, month_start, month_end = _month_bounds(month)
        return {"endpoint": endpoint, "collection": "employees", "explain": _explain(
            db.employees, pipeline=_monthly_stats_pipeline(emp_code, start, end, month_start, month_end),
        )}
    if endpoint == "department_summary":
        start, end, _, _ = _month_bounds(month)
        return {"endpoint": endpoint, "collection": "employees", "explain": _explain(
            db.employees, pipeline=_department_summary_pipeline(start, end, department),
        )}
    if endpoint == "late_leaderboard":
        start, end, _, _ = _month_bounds(month)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _explain(
            db.attendance_logs, pipeline=_leaderboard_pipeline(start, end, department, limit),
        )}
    if department is None or from_date is None or to_date is None:
        raise HTTPException(422, "department, from and to are required")
    start_date = _request_date(from_date)
    end_date = _request_date(to_date)
    if end_date < start_date or (end_date - start_date).days + 1 > 92:
        raise HTTPException(422, "invalid trend date range")
    return {"endpoint": endpoint, "collection": "employees", "explain": _explain(
        db.employees, pipeline=_trend_pipeline(department, start_date, end_date),
    )}
