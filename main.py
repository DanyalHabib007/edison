import uvicorn
from fastapi import FastAPI, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiosqlite
from datetime import datetime, timedelta
import csv
import io
import os
import secrets

# --- SECURITY IMPORTS ---
from passlib.context import CryptContext
from jose import JWTError, jwt

app = FastAPI()

# --- CONFIGURATION ---
SECRET_KEY = secrets.token_hex(32)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 10080  # 7 days

DB_NAME = "khatabook.db"

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- SECURITY SETUP ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- IN-MEMORY USER STORE ---
users_db = {}

# --- PASSWORD HELPERS (bcrypt-safe) ---
def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password[:72], hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password[:72])

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# --- DATABASE SETUP ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                type TEXT NOT NULL,
                description TEXT,
                date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (customer_id) REFERENCES customers (id)
            )
        """)
        await db.commit()

@app.on_event("startup")
async def startup_event():
    await init_db()
    users_db["admin"] = get_password_hash("edison.ele@123")
    print("✅ AUTH READY: admin user loaded")

# --- AUTH HELPER ---
def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        scheme, _, param = token.partition(" ")
        payload = jwt.decode(param, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
    except JWTError:
        return None
    return username if username in users_db else None

# --- CUSTOMER BALANCE ---
async def get_customer_balance(customer_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT SUM(amount) FROM transactions WHERE customer_id=? AND type='GAVE'",
            (customer_id,)
        )
        gave = (await cur.fetchone())[0] or 0

        cur = await db.execute(
            "SELECT SUM(amount) FROM transactions WHERE customer_id=? AND type='GOT'",
            (customer_id,)
        )
        got = (await cur.fetchone())[0] or 0

        return gave - got

# --- AUTH ROUTES ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username not in users_db:
        return templates.TemplateResponse("login.html", {"request": request, "error": "User not found"})

    if not verify_password(password, users_db[username]):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid password"})

    token = create_access_token({"sub": username})
    response = RedirectResponse("/", status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        "access_token",
        f"Bearer {token}",
        httponly=True,
        samesite="lax"
    )
    return response

@app.get("/logout")
async def logout():
    response = RedirectResponse("/login")
    response.delete_cookie("access_token")
    return response

# --- DASHBOARD ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not get_current_user(request):
        return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM customers")
        customers = await cur.fetchall()

    data = []
    total = 0
    for c in customers:
        bal = await get_customer_balance(c["id"])
        if bal > 0:
            total += bal
        data.append({**dict(c), "balance": bal})

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "customers": data, "total_to_collect": total}
    )

# --- CUSTOMER CRUD ---
@app.post("/add_customer")
async def add_customer(request: Request, name: str = Form(...), phone: str = Form(...)):
    if not get_current_user(request):
        return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO customers (name, phone) VALUES (?,?)", (name, phone))
        await db.commit()
    return RedirectResponse("/", status_code=303)

@app.get("/customer/{customer_id}", response_class=HTMLResponse)
async def view_customer(request: Request, customer_id: int):
    if not get_current_user(request):
        return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM customers WHERE id=?", (customer_id,))
        customer = await cur.fetchone()
        if not customer:
            raise HTTPException(404)

        cur = await db.execute(
            "SELECT * FROM transactions WHERE customer_id=? ORDER BY date DESC",
            (customer_id,)
        )
        txns = await cur.fetchall()

    balance = await get_customer_balance(customer_id)
    return templates.TemplateResponse(
        "customer.html",
        {"request": request, "customer": customer, "transactions": txns, "balance": balance}
    )

@app.post("/add_transaction")
async def add_transaction(
    request: Request,
    customer_id: int = Form(...),
    amount: float = Form(...),
    type: str = Form(...),
    description: str = Form("")
):
    if not get_current_user(request):
        return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO transactions (customer_id, amount, type, description) VALUES (?,?,?,?)",
            (customer_id, amount, type, description)
        )
        await db.commit()

    return RedirectResponse(f"/customer/{customer_id}", status_code=303)

# --- EXPORT ---
@app.get("/download_db")
async def download_db(request: Request):
    if not get_current_user(request):
        return RedirectResponse("/login")

    if os.path.exists(DB_NAME):
        name = f"khatabook_backup_{datetime.now():%Y%m%d_%H%M}.db"
        return FileResponse(DB_NAME, filename=name)

    return RedirectResponse("/")

# --- RUN ---
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080)
