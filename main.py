
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from pathlib import Path
import os, asyncio, json, time

from database import get_db, init_db
from models import Employee, Result, Team

app = FastAPI()

ADMIN_PASSWORD = "admin"

SECRET = os.getenv("SESSION_SECRET", "dev-secret")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET,
    session_cookie="ct_session",
    same_site="lax",
    https_only=COOKIE_SECURE,
    max_age=60*60*24*30,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Цели по командам
# Team targets are stored in the database (teams.target_daily / teams.target_weekly).
# These are just fallback defaults for older databases / safety.
DEFAULT_TEAM_TARGETS = {
    "left":  {"daily": 4_000_000,  "weekly": 24_000_000},
    "right": {"daily": 4_200_000,  "weekly": 25_000_000},
}

DAYS_ORDER = ["ПТ","СБ","ПН","ВТ","СР","ЧТ"]
@app.on_event("startup")
def _startup():
    init_db()

class SSEHub:
    def __init__(self):
        self.clients = set()
        self.lock = asyncio.Lock()
    async def connect(self):
        q = asyncio.Queue()
        async with self.lock:
            self.clients.add(q)
        return q
    async def disconnect(self, q):
        async with self.lock:
            self.clients.discard(q)
    async def broadcast(self, payload: dict):
        async with self.lock:
            for q in list(self.clients):
                try: q.put_nowait(payload)
                except Exception: pass
hub = SSEHub()

def is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))

def parse_amount(s: str) -> int:
    if s is None: return 0
    s = str(s).strip().lower().replace(' ', '')
    s = s.replace('kk','кк').replace('k','к')
    sign = 1
    if s.startswith('+'): s = s[1:]
    elif s.startswith('-'): sign = -1; s = s[1:]
    if s.endswith('кк'):
        num = s[:-2].replace(',', '.'); return int(float(num)*1_000_000)*sign
    if s.endswith('к'):
        num = s[:-1].replace(',', '.'); return int(float(num)*1_000)*sign
    s = s.replace(',', '.')
    try:
        return int(float(s)) * sign if '.' in s else int(s) * sign
    except ValueError:
        return 0

def recalc_employee_total(db: Session, employee_id: int) -> int:
    total = db.query(func.sum(Result.amount)).filter(Result.employee_id == employee_id).scalar() or 0
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if emp:
        emp.total_sum = total
        db.commit()
    return total

def team_aggregates(db: Session, team_key: str):
    totals_by_day = {d: 0 for d in DAYS_ORDER}
    q = (db.query(Result.day, func.sum(Result.amount))
         .join(Employee, Employee.id == Result.employee_id)
         .filter(Employee.team_key == team_key)
         .group_by(Result.day).all())
    for day, sm in q:
        if day in totals_by_day: totals_by_day[day] = sm or 0
    grand_total = sum(totals_by_day.values())
    employees = (db.query(Employee)
                 .filter(Employee.team_key == team_key)
                 .order_by(Employee.total_sum.desc())
                 .all())
    team = db.query(Team).filter(Team.key == team_key).first()
    team_name = team.name if team else team_key
    fallback = DEFAULT_TEAM_TARGETS.get(team_key, {"daily": 0, "weekly": 0})
    target_daily = int(team.target_daily) if team is not None and getattr(team, "target_daily", None) is not None else int(fallback.get("daily", 0))
    target_weekly = int(team.target_weekly) if team is not None and getattr(team, "target_weekly", None) is not None else int(fallback.get("weekly", 0))
    return {
        "name": team_name,
        "employees": employees,
        "totals_by_day": totals_by_day,
        "grand_total": grand_total,
        "target_daily": target_daily,
        "target_weekly": target_weekly,
    }

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    left = team_aggregates(db, "left")
    right = team_aggregates(db, "right")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "days": DAYS_ORDER,
        "left": left,
        "right": right,
    })

@app.get("/admin", response_class=HTMLResponse)
def admin_get(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return templates.TemplateResponse("admin_login.html", {"request": request})
    employees = db.query(Employee).order_by(Employee.id.asc()).all()
    results = db.query(Result).all()
    res_map = {}
    for r in results:
        res_map.setdefault(r.employee_id, {})[r.day] = r.amount
    teams = db.query(Team).order_by(Team.key.asc()).all()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "employees": employees, "days": DAYS_ORDER,
        "res_map": res_map, "teams": teams,
    })

@app.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)):
    if password.strip() == ADMIN_PASSWORD:
        request.session["is_admin"] = True
        return RedirectResponse(url="/admin", status_code=302)
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": "Неверный пароль"})

@app.post("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)

async def _reload():
    await hub.broadcast({"event":"reload","t":time.time()})

@app.post("/admin/team/rename")
async def rename_team(request: Request, key: str = Form(...), name: str = Form(...), db: Session = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"status":"error","message":"Требуется авторизация"}, status_code=403)
    t = db.query(Team).filter(Team.key == key).first()
    if not t:
        t = Team(key=key, name=name.strip()); db.add(t)
    else:
        t.name = name.strip()
    db.commit()
    await _reload()
    return JSONResponse({"status":"success","message":"Название команды сохранено"})


@app.post("/admin/team/targets")
async def set_team_targets(
    request: Request,
    key: str = Form(...),
    target_daily: str = Form(...),
    target_weekly: str = Form(...),
    db: Session = Depends(get_db),
):
    if not is_admin(request):
        return JSONResponse({"status":"error","message":"Требуется авторизация"}, status_code=403)

    td = parse_amount(target_daily)
    tw = parse_amount(target_weekly)
    if td < 0: td = 0
    if tw < 0: tw = 0

    t = db.query(Team).filter(Team.key == key).first()
    if not t:
        t = Team(key=key, name=key, target_daily=td, target_weekly=tw)
        db.add(t)
    else:
        t.target_daily = td
        t.target_weekly = tw

    db.commit()
    await _reload()
    return JSONResponse({"status":"success","message":"Цели команды сохранены"})

@app.post("/admin/employee/add")
async def employee_add(request: Request, name: str = Form(...), team_key: str = Form("left"), db: Session = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"status":"error","message":"Требуется авторизация"}, status_code=403)
    name = name.strip()
    if not name: return JSONResponse({"status":"error","message":"Имя пустое"}, status_code=400)
    if team_key not in ("left","right"): team_key = "left"
    emp = Employee(name=name, team_key=team_key, total_sum=0)
    db.add(emp); db.commit()
    for d in DAYS_ORDER:
        db.add(Result(employee_id=emp.id, day=d, amount=0))
    db.commit()
    await _reload()
    return JSONResponse({"status":"success","message":"Сотрудник добавлен"})

@app.post("/admin/employee/rename")
async def employee_rename(request: Request, employee_id: int = Form(...), name: str = Form(...), db: Session = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"status":"error","message":"Требуется авторизация"}, status_code=403)
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp: return JSONResponse({"status":"error","message":"Сотрудник не найден"}, status_code=404)
    emp.name = name.strip(); db.commit()
    await _reload()
    return JSONResponse({"status":"success","message":"Имя обновлено"})

@app.post("/admin/employee/delete")
async def employee_delete(request: Request, employee_id: int = Form(...), db: Session = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"status":"error","message":"Требуется авторизация"}, status_code=403)
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp: return JSONResponse({"status":"error","message":"Сотрудник не найден"}, status_code=404)
    db.delete(emp); db.commit()
    await _reload()
    return JSONResponse({"status":"success","message":"Сотрудник удалён"})

@app.post("/admin/employee/set_team")
async def set_team(request: Request, employee_id: int = Form(...), team_key: str = Form(...), db: Session = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"status":"error","message":"Требуется авторизация"}, status_code=403)
    if team_key not in ("left","right"): return JSONResponse({"status":"error","message":"Некорректная команда"}, status_code=400)
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp: return JSONResponse({"status":"error","message":"Сотрудник не найден"}, status_code=404)
    emp.team_key = team_key; db.commit()
    await _reload()
    return JSONResponse({"status":"success","message":"Команда обновлена"})

@app.post("/admin/result/update")
async def update_result(request: Request, employee_id: int = Form(...), day: str = Form(...), amount: str = Form(...), db: Session = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"status":"error","message":"Требуется авторизация"}, status_code=403)
    day = day.strip()
    if day not in DAYS_ORDER: return JSONResponse({"status":"error","message":"Некорректный день"}, status_code=400)
    amt = parse_amount(amount)
    rec = db.query(Result).filter(Result.employee_id == employee_id, Result.day == day).first()
    if not rec: rec = Result(employee_id=employee_id, day=day, amount=0); db.add(rec)
    rec.amount = max(0, amt); db.commit()
    recalc_employee_total(db, employee_id)
    await _reload()
    return JSONResponse({"status":"success","message":"Сумма обновлена"})

@app.post("/admin/result/increment")
async def increment_result(request: Request, employee_id: int = Form(...), day: str = Form(...), delta: str = Form(...), db: Session = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"status":"error","message":"Требуется авторизация"}, status_code=403)
    day = day.strip()
    if day not in DAYS_ORDER: return JSONResponse({"status":"error","message":"Некорректный день"}, status_code=400)
    d = parse_amount(delta)
    rec = db.query(Result).filter(Result.employee_id == employee_id, Result.day == day).first()
    if not rec: rec = Result(employee_id=employee_id, day=day, amount=0); db.add(rec)
    rec.amount = max(0, (rec.amount or 0) + d); db.commit()
    recalc_employee_total(db, employee_id)
    await _reload()
    return JSONResponse({"status":"success","message":"Изменено на дельту"})

@app.post("/admin/reset_all")
async def reset_all(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"status":"error","message":"Требуется авторизация"}, status_code=403)
    db.query(Result).update({Result.amount: 0})
    db.query(Employee).update({Employee.total_sum: 0})
    db.commit()
    await _reload()
    return JSONResponse({"status":"success","message":"Вся статистика обнулена"})

@app.get("/events")
async def events(request: Request):
    q = await hub.connect()
    async def gen():
        try:
            yield f"data: {json.dumps({'event':'hello','t': time.time()})}\n\n"
            while True:
                if await request.is_disconnected(): break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield f": keep-alive\n\n"
        finally:
            await hub.disconnect(q)
    return StreamingResponse(gen(), media_type="text/event-stream")

