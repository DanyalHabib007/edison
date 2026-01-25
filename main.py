import uvicorn
from fastapi import FastAPI, Request, Form, HTTPException, Path, status, Depends, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiosqlite
from datetime import datetime, timedelta
import csv
import io
import os
import shutil
# --- NEW SECURITY IMPORTS ---
from passlib.context import CryptContext
from jose import JWTError, jwt
from typing import Optional

app = FastAPI()

# --- CONFIGURATION ---
SECRET_KEY = "gr6565e1rg51er5g1e1r" 
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 10080 # 7 Days

DB_NAME = "khatabook.db"

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- SECURITY SETUP ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 1. THE DICTIONARY (Replaces User Table)
users_db = {} 

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# --- DATABASE SETUP (Only Customers/Transactions) ---
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
    
    # 2. POPULATE DICTIONARY ON STARTUP
    # This creates the user in memory every time you restart the server
    # Username: admin, Password: edison.ele@123
    users_db["admin"] = get_password_hash("edison.ele@123")
    print("--- AUTH READY: User 'admin' created in memory ---")

# --- AUTH HELPER ---
def get_current_user(request: Request):
    """Checks for cookie and validates against Dictionary"""
    token = request.cookies.get("access_token")
    if not token:
        return None
    
    try:
        scheme, _, param = token.partition(" ")
        payload = jwt.decode(param, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
    except JWTError:
        return None
    
    # Check if this user exists in our Dictionary
    if username in users_db:
        return username
    return None

# --- HELPER: CUSTOMER BALANCE ---
async def get_customer_balance(customer_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT SUM(amount) FROM transactions WHERE customer_id = ? AND type = 'GAVE'", (customer_id,))
        gave = (await cursor.fetchone())[0] or 0.0
        
        cursor = await db.execute("SELECT SUM(amount) FROM transactions WHERE customer_id = ? AND type = 'GOT'", (customer_id,))
        got = (await cursor.fetchone())[0] or 0.0
        
        return gave - got

# --- AUTH ROUTES ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    # 1. Check Dictionary
    if username not in users_db:
        return templates.TemplateResponse("login.html", {"request": request, "error": "User not found"})
    
    stored_hash = users_db[username]
    
    # 2. Verify Password
    if not verify_password(password, stored_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid password"})
    
    # 3. Create Token & Redirect
    access_token = create_access_token(data={"sub": username})
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("access_token")
    return response

# --- APP ROUTES (PROTECTED) ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = None, sort: str = "date_desc"):
    # PROTECT ROUTE
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if q:
            search_query = f"%{q}%"
            cursor = await db.execute("SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ?", (search_query, search_query))
        else:
            cursor = await db.execute("SELECT * FROM customers") 
        customers = await cursor.fetchall()
    
    customer_list = []
    total_gave = 0.0
    
    async with aiosqlite.connect(DB_NAME) as db:
        for row in customers:
            cust = dict(row)
            balance = await get_customer_balance(cust['id'])
            cust['balance'] = balance
            
            cursor = await db.execute("SELECT date FROM transactions WHERE customer_id = ? ORDER BY date DESC LIMIT 1", (cust['id'],))
            last_trans = await cursor.fetchone()
            cust['last_activity'] = last_trans[0] if last_trans else "1970-01-01 00:00:00"

            customer_list.append(cust)
            if balance > 0:
                total_gave += balance

    # Sorting
    if sort == 'bal_high': customer_list.sort(key=lambda x: x['balance'], reverse=True)
    elif sort == 'bal_low': customer_list.sort(key=lambda x: x['balance'])
    elif sort == 'date_desc': customer_list.sort(key=lambda x: x['last_activity'], reverse=True)
    elif sort == 'date_asc': customer_list.sort(key=lambda x: x['last_activity'])
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "customers": customer_list,
        "total_to_collect": total_gave,
        "q": q,
        "sort": sort,
        "user": user
    })

@app.post("/add_customer")
async def add_customer(request: Request, name: str = Form(...), phone: str = Form(...)):
    if not get_current_user(request): return RedirectResponse("/login")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO customers (name, phone) VALUES (?, ?)", (name, phone))
        await db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.get("/customer/{customer_id}", response_class=HTMLResponse)
async def view_customer(request: Request, customer_id: int):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,))
        customer = await cursor.fetchone()
        
        if not customer: raise HTTPException(status_code=404)
        
        cursor = await db.execute("SELECT * FROM transactions WHERE customer_id = ? ORDER BY date DESC", (customer_id,))
        transactions = await cursor.fetchall()
        
    balance = await get_customer_balance(customer_id)
    
    return templates.TemplateResponse("customer.html", {
        "request": request,
        "customer": customer,
        "transactions": transactions,
        "balance": balance,
        "user": user
    })

@app.post("/add_transaction")
async def add_transaction(request: Request, customer_id: int = Form(...), amount: float = Form(...), type: str = Form(...), description: str = Form("")):
    if not get_current_user(request): return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO transactions (customer_id, amount, type, description) VALUES (?, ?, ?, ?)", (customer_id, amount, type, description))
        await db.commit()
    return RedirectResponse(url=f"/customer/{customer_id}", status_code=303)

@app.post("/edit_transaction")
async def edit_transaction(request: Request, transaction_id: int = Form(...), customer_id: int = Form(...), amount: float = Form(...), description: str = Form(""), type: str = Form(...)):
    if not get_current_user(request): return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE transactions SET amount = ?, description = ?, type = ? WHERE id = ?", (amount, description, type, transaction_id))
        await db.commit()
    return RedirectResponse(url=f"/customer/{customer_id}", status_code=303)

@app.post("/delete_transaction/{transaction_id}")
async def delete_transaction(request: Request, transaction_id: int):
    if not get_current_user(request): return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT customer_id FROM transactions WHERE id = ?", (transaction_id,))
        row = await cursor.fetchone()
        if row:
            await db.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
            await db.commit()
            return RedirectResponse(url=f"/customer/{row[0]}", status_code=303)
    return RedirectResponse(url="/", status_code=303)

@app.post("/edit_customer")
async def edit_customer(request: Request, customer_id: int = Form(...), name: str = Form(...), phone: str = Form(...)):
    if not get_current_user(request): return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE customers SET name = ?, phone = ? WHERE id = ?", (name, phone, customer_id))
        await db.commit()
    return RedirectResponse(url=f"/customer/{customer_id}", status_code=303)

@app.post("/delete_customer/{customer_id}")
async def delete_customer(request: Request, customer_id: int):
    if not get_current_user(request): return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM transactions WHERE customer_id = ?", (customer_id,))
        await db.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
        await db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.get("/customer/{customer_id}/download")
async def download_statement(request: Request, customer_id: int):
    if not get_current_user(request): return RedirectResponse("/login")

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,))
        customer = await cursor.fetchone()
        if not customer: raise HTTPException(status_code=404)

        cursor = await db.execute("SELECT * FROM transactions WHERE customer_id = ? ORDER BY date DESC", (customer_id,))
        transactions = await cursor.fetchall()
        balance = await get_customer_balance(customer_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Description", "Type", "Amount", "Balance Context"])
    for t in transactions:
        writer.writerow([t['date'], t['description'], t['type'], t['amount'], "You Gave" if t['type'] == 'GAVE' else "You Received"])
    writer.writerow([])
    writer.writerow(["", "", "NET BALANCE", balance])

    output.seek(0)
    headers = {'Content-Disposition': f'attachment; filename="statement_{customer["name"]}.csv"'}
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers=headers)

@app.get("/download_db")
async def download_db(request: Request):
    if not get_current_user(request): return RedirectResponse("/login")
    if os.path.exists(DB_NAME):
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        return FileResponse(path=DB_NAME, filename=f"backup_khatabook_{timestamp}.db", media_type='application/octet-stream')
    return RedirectResponse(url="/")

@app.post("/restore_db")
async def restore_db(request: Request, file: UploadFile = File(...)):
    # 1. Protect the route
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")

    # 2. Basic Validation (Check extension)
    if not file.filename.endswith(".db"):
        # You could add an error message logic here, 
        # but for now we just reload dashboard
        return RedirectResponse(url="/", status_code=303)

    # 3. Overwrite the database file
    # We copy the uploaded file content directly over 'khatabook.db'
    try:
        with open(DB_NAME, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        print(f"Error restoring database: {e}")
        # In a production app, you'd want to flash an error message here
    
    return RedirectResponse(url="/", status_code=303)

if __name__ == "__main__":
    uvicorn.run("main:app")
