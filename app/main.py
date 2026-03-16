from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv
from groq import Groq
from supabase import create_client
import os
import json
import re

load_dotenv(dotenv_path=".env")

app = FastAPI()
templates = Jinja2Templates(directory="templates")

groq_api_key = os.getenv("GROQ_API_KEY")
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if not groq_api_key:
    raise ValueError("Missing GROQ_API_KEY in .env")

if not supabase_url or not supabase_key:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY in .env")

client = Groq(api_key=groq_api_key)
supabase = create_client(supabase_url, supabase_key)


class MessageInput(BaseModel):
    message: str


def extract_json(text: str):
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError("Could not parse JSON from model response")


def process_transaction(message: str):
    prompt = f"""
Extract transaction data from this merchant message.

Return JSON only with exactly these keys:
type, product, quantity, price, total

Rules:
- type must be either "sale" or "purchase"
- product should be plain lowercase text
- quantity must be an integer
- price must be a number
- total must equal quantity * price

Merchant message:
"{message}"
"""

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {
                "role": "system",
                "content": "You extract transaction data and return valid JSON only."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0
    )

    content = response.choices[0].message.content
    parsed = extract_json(content)

    supabase.table("transactions").insert({
        "raw_message": message,
        "type": parsed["type"],
        "product": parsed["product"],
        "quantity": parsed["quantity"],
        "price": parsed["price"],
        "total": parsed["total"]
    }).execute()

    sales = supabase.table("transactions").select("total").eq("type", "sale").execute()
    total_sales = sum(float(item["total"]) for item in sales.data)

    return parsed, total_sales

def get_dashboard_data():
    transactions_response = supabase.table("transactions").select("*").order("created_at", desc=True).execute()
    transactions = transactions_response.data if transactions_response.data else []

    total_sales = sum(
        float(item["total"]) for item in transactions
        if item.get("type") == "sale" and item.get("total") is not None
    )

    total_purchases = sum(
        float(item["total"]) for item in transactions
        if item.get("type") == "purchase" and item.get("total") is not None
    )

    estimated_profit = total_sales - total_purchases

    inventory = {}
    sales_by_product = {}

    for item in transactions:
        product = item.get("product")
        qty = item.get("quantity", 0)

        if not product:
            continue

        if product not in inventory:
            inventory[product] = 0

        if item.get("type") == "purchase":
            inventory[product] += qty
        elif item.get("type") == "sale":
            inventory[product] -= qty

            if product not in sales_by_product:
                sales_by_product[product] = 0
            sales_by_product[product] += qty

    low_stock = []
    for product, qty in inventory.items():
        if qty <= 3:
            low_stock.append({
                "product": product,
                "quantity": qty
            })

    top_product = None
    if sales_by_product:
        top_product = max(sales_by_product, key=sales_by_product.get)

    transaction_count = len(transactions)

    score = 0
    score += min(transaction_count * 8, 40)

    if total_sales > 0:
        score += min(int(total_sales / 100), 20)

    if estimated_profit > 0:
        score += min(int(estimated_profit / 100), 20)

    if transaction_count >= 5:
        score += 10

    if top_product:
        score += 10

    readiness_score = min(score, 100)

    estimated_loan_amount = readiness_score * 100

    insights = []

    if top_product:
        insights.append(f"{top_product.title()} is your top selling product today.")

    if estimated_profit > 0:
        insights.append(f"Your business is currently profitable with an estimated profit of KES {estimated_profit:.0f}.")
    elif estimated_profit < 0:
        insights.append(f"Your purchases are higher than your sales so far. Estimated profit is KES {estimated_profit:.0f}.")
    else:
        insights.append("Your sales and purchases are currently balanced.")

    if low_stock:
        low_item = low_stock[0]
        insights.append(f"{low_item['product'].title()} is running low with only {low_item['quantity']} left.")

    if readiness_score >= 70:
        insights.append("Your transaction history shows strong financial readiness for micro-credit.")
    elif readiness_score >= 40:
        insights.append("Your business is building a usable financial history, but more transaction data will improve credit readiness.")
    else:
        insights.append("Keep recording transactions to build a stronger financial profile.")

    if not transactions:
        insights = ["Start by recording a sale or purchase to generate business insights and build financial readiness."]

    return {
        "transactions": transactions[:10],
        "total_sales": total_sales,
        "total_purchases": total_purchases,
        "estimated_profit": estimated_profit,
        "low_stock": low_stock,
        "top_product": top_product,
        "insights": insights,
        "readiness_score": readiness_score,
        "estimated_loan_amount": estimated_loan_amount
    }

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    try:
        dashboard = get_dashboard_data()

        return templates.TemplateResponse("index.html", {
            "request": request,
            "transaction": None,
            "total_sales": dashboard["total_sales"],
            "total_purchases": dashboard["total_purchases"],
            "estimated_profit": dashboard["estimated_profit"],
            "low_stock": dashboard["low_stock"],
            "transactions": dashboard["transactions"],
            "top_product": dashboard["top_product"],
            "insights": dashboard["insights"],
            "readiness_score": dashboard["readiness_score"],
            "estimated_loan_amount": dashboard["estimated_loan_amount"]
        })
    except Exception as e:
        return HTMLResponse(
            content=f"<h1>Error loading home page</h1><pre>{str(e)}</pre>",
            status_code=500
        )

@app.post("/", response_class=HTMLResponse)
def submit_message(request: Request, message: str = Form(...)):
    try:
        parsed, _ = process_transaction(message)
        dashboard = get_dashboard_data()

        return templates.TemplateResponse("index.html", {
            "request": request,
            "transaction": parsed,
            "total_sales": dashboard["total_sales"],
            "total_purchases": dashboard["total_purchases"],
            "estimated_profit": dashboard["estimated_profit"],
            "low_stock": dashboard["low_stock"],
            "transactions": dashboard["transactions"],
            "top_product": dashboard["top_product"],
            "insights": dashboard["insights"],
            "readiness_score": dashboard["readiness_score"],
            "estimated_loan_amount": dashboard["estimated_loan_amount"]
        })
    except Exception as e:
        return HTMLResponse(
            content=f"<h1>Error submitting transaction</h1><pre>{str(e)}</pre>",
            status_code=500
        )

@app.post("/extract")
def extract_transaction(data: MessageInput):
    try:
        parsed, total_sales = process_transaction(data.message)
        return {
            "success": True,
            "transaction": parsed,
            "summary": {
                "todays_sales": total_sales
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))