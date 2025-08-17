from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from typing import List, Any
from datetime import date, datetime, timedelta
import os
import uuid
import psycopg2
import psycopg2.extras
from decimal import Decimal

# Load .env file at the start of the application
load_dotenv()


# --- Helper to read multiple possible env keys ---
def env_first(*keys):
    """Return the value of the first environment variable found in the list of keys."""
    for k in keys:
        v = os.getenv(k)
        if v:
            return v
    return None


# --- DB config and sanity check ---
DB_CONFIG = {
    "user": env_first("PGUSER", "user"),
    "password": env_first("PGPASSWORD", "password"),
    "host": env_first("PGHOST", "host"),
    "port": env_first("PGPORT", "port"),
    "dbname": env_first("PGDATABASE", "dbname"),
}

# Ensure all required DB variables are present
missing = [k for k, v in DB_CONFIG.items() if not v]
if missing:
    raise RuntimeError(
        f"Missing DB env variables for: {missing}. Set PGUSER/PGPASSWORD/PGHOST/PGPORT/PGDATABASE or user/password/host/port/dbname in your .env"
    )

# --- FastAPI app initialization ---
app = FastAPI()

# --- CORS (adjust origins if needed) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Pydantic Models ---
# The Pydantic model for Active Clients, updated with correct date/datetime types
class ActiveClientResponse(BaseModel):
    """
    Pydantic model for an active client.
    Note: date and datetime fields are now of the correct Python type.
    Pydantic and FastAPI will handle the conversion to ISO 8601 strings in JSON.
    """

    id: int
    client_name: str
    contact_phone: str | None = None
    project_title: str
    project_description: str | None = None
    delivery_date: date | None = None  # Changed from str to date
    # advance_token is a string in the DB, so it should be a string here.
    # Note: For financial data, it's generally better to use Decimal in Python and NUMERIC/DECIMAL in the DB.
    advance_token: str | None = None
    project_manager: str | None = None
    project_reference: str | None = None
    status: str | None = None
    created_at: datetime  # Changed from str to datetime
    updated_at: datetime  # Changed from str to datetime

    class Config:
        # Pydantic v2 uses 'from_attributes' instead of 'orm_mode'
        from_attributes = True


class AdminStatsResponse(BaseModel):
    """Pydantic model for the admin stats dashboard."""

    new_enquiries: int
    active_projects: int
    completed_projects: int


class ActiveClientsResponse(BaseModel):
    """Response model for a list of active clients."""

    status: str
    data: List[ActiveClientResponse]


class MessageResponse(BaseModel):
    """Generic response model for status messages."""

    status: str
    message: str


class BlogResponse(BaseModel):
    """Pydantic model for a blog post."""

    id: int
    title: str
    slug: str
    excerpt: str
    content: str
    image_url: str = Field(..., alias="imageUrl")
    date: date

    class Config:
        from_attributes = True
        populate_by_name = True


class Item(BaseModel):
    """Pydantic model for a single billing item."""

    service: str
    rate: str
    quantity: int
    price: str


class BillCreateRequest(BaseModel):
    """Request model for creating a new bill."""

    customerName: str | None
    customerId: str | None
    customerPhone: str | None
    billNoSuffix: str | None
    date: date | None
    paymentMode: str | None
    billPrefix: str | None
    items: List[Item]
    grandTotal: str


class EnquiryCreateRequest(BaseModel):
    """Request model for adding a new enquiry."""

    name: str
    email: str
    phone: str | None = None
    service: str | None = None
    budget: str
    timeline: str
    idea: str
    description: str
    reference: str | None = None
    consent: bool


class BlogCreateRequest(BaseModel):
    """Request model for creating a new blog post."""

    title: str
    slug: str
    excerpt: str
    content: str
    image_url: str
    date: date


class BillResponse(BaseModel):
    """Response model for a bill."""

    id: int
    bill_no: str
    customer_name: str | None
    customer_contact: str | None
    products: List[Any]
    total_amount: float
    payment_mode: str | None
    billing_date: date

    class Config:
        from_attributes = True


# --- Letterhead Models ---
class LetterCreateRequest(BaseModel):
    """Request model for creating a new letter."""

    date: date
    ref_number: str
    issued_to: str
    issued_by: str
    subject: str
    content: str


class LetterResponse(BaseModel):
    """Response model for a letter."""

    id: int
    date: date
    ref_number: str
    issued_to: str
    issued_by: str
    subject: str
    content: str

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    """Request model for user login."""

    email: str
    password: str


class SessionValidationRequest(BaseModel):
    """Request model for session validation and logout."""

    email: str
    session_id: str


class ProjectApproveRequest(BaseModel):
    """Request model for approving an enquiry and converting to a project."""

    name: str | None
    email: str | None
    phone: str | None = None
    service: str | None = None
    budget: str | None = None
    timeline: str | None = None
    idea: str | None = None
    description: str | None = None
    reference: str | None = None
    consent: bool | None = None
    submitted_at: datetime

    # New fields required for project approval
    delivery_date: date
    billing_details: str
    project_title: str
    project_description: str
    project_reference: str | None = None
    project_manager: str


# ** New Quick Contact Models **
class QuickContactCreateRequest(BaseModel):
    """Request model for a quick contact form submission."""

    name: str
    phone: str
    subject: str | None = None
    message: str | None = None


class QuickContactResponse(BaseModel):
    """Response model for a quick contact."""

    id: int
    name: str
    phone: str
    subject: str | None = None
    message: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class QuickContactApproveRequest(BaseModel):
    """Request model for approving a quick contact."""

    name: str | None = None
    phone: str | None = None
    project_title: str
    project_description: str
    delivery_date: date
    billing_details: str
    project_manager: str
    reference: str | None = None


# --- Session Management ---
# WARNING: This is an in-memory session store. It is not persistent and will
# lose all sessions if the server restarts. For production, use a database or
# a dedicated cache like Redis.
active_sessions = {}
SESSION_DURATION_MINUTES = 1080


# --- DB helper ---
def get_db():
    """Return a new psycopg2 connection with RealDictCursor."""
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


def _serialize_value(v):
    """Helper to convert database types (Decimal, date, datetime) to JSON-serializable types."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def serialize_row_rename_image(row: dict):
    """Serialize a RealDictRow and rename image_url->imageUrl."""
    out = {}
    for k, v in row.items():
        new_key = "imageUrl" if k == "image_url" else k
        out[new_key] = _serialize_value(v)
    return out


# --- Helper for fetching user from DB ---
def get_user_by_email(email: str):
    """Fetch a user from the database by email."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE email = %s;", (email,))
                return cur.fetchone()
    except Exception as e:
        # Log the error for debugging
        print(f"Database error while fetching user: {e}")
        return None


# === Endpoints ===


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    """Endpoint to check server and database status."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT NOW() AS now;")
                row = cur.fetchone()
                db_time = row["now"].isoformat() if row and row.get("now") else None
        db_status = "alive"
    except Exception as e:
        db_status = f"error - {e}"
        db_time = None

    return {
        "status": "Server is alive!",
        "db_status": db_status,
        "time": datetime.utcnow().isoformat(),
        "db_time": db_time,
    }


# === Auth Endpoints ===
@app.post("/login")
async def login(request: LoginRequest):
    """
    User login endpoint with simple password check.
    """
    user = get_user_by_email(request.email)

    # Check if user exists and password is correct (simple string comparison)
    if not user or user.get("password") != request.password:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    session_id = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(minutes=SESSION_DURATION_MINUTES)
    active_sessions[user["email"]] = {
        "session_id": session_id,
        "expires_at": expires_at,
    }

    return {
        "status": "success",
        "name": user["email"],
        "user_id": user["id"],
        "session_id": session_id,
    }


@app.post("/validate-session")
async def validate_session(req: SessionValidationRequest):
    """Validates an active user session."""
    session = active_sessions.get(req.email)
    if (
        not session
        or session["session_id"] != req.session_id
        or session["expires_at"] < datetime.utcnow()
    ):
        active_sessions.pop(req.email, None)
        return JSONResponse(content={"status": "invalid"}, status_code=401)
    return {"status": "valid"}


@app.post("/userlogout")
async def user_logout(req: SessionValidationRequest):
    """Logs a user out by removing their session."""
    active_sessions.pop(req.email, None)
    return {"status": "logged_out"}


# === Blog Endpoints ===
@app.get("/api/blogs")
@app.get("/blogs")
def get_blogs():
    """Fetches all blog posts from the database."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, slug, excerpt, content, image_url, date FROM blogs ORDER BY date DESC;"
                )
                rows = cur.fetchall() or []
        blogs = [serialize_row_rename_image(dict(r)) for r in rows]
        return blogs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/addblog", status_code=status.HTTP_201_CREATED)
async def add_blog(request: BlogCreateRequest):
    """Adds a new blog post to the database."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM blogs WHERE slug = %s;", (request.slug,))
                if cur.fetchone():
                    raise HTTPException(status_code=400, detail="Slug already exists.")
                cur.execute(
                    """
                    INSERT INTO blogs (title, slug, excerpt, content, image_url, date)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        request.title,
                        request.slug,
                        request.excerpt,
                        request.content,
                        request.image_url,
                        request.date,
                    ),
                )
            conn.commit()
        return {"status": "success", "message": "Blog added successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/editblog/{slug}")
async def edit_blog(slug: str, updated_blog: BlogCreateRequest):
    """Updates an existing blog post."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM blogs WHERE slug = %s;", (slug,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Blog not found")
                cur.execute(
                    """
                    UPDATE blogs
                    SET title=%s, slug=%s, excerpt=%s, content=%s, image_url=%s, date=%s
                    WHERE slug = %s
                    """,
                    (
                        updated_blog.title,
                        updated_blog.slug,
                        updated_blog.excerpt,
                        updated_blog.content,
                        updated_blog.image_url,
                        updated_blog.date,
                        slug,
                    ),
                )
            conn.commit()
        return {"status": "success", "message": "Blog updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/deleteblog/{slug}")
async def delete_blog(slug: str):
    """Deletes a blog post by its slug."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM blogs WHERE slug = %s;", (slug,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Blog not found")
                cur.execute("DELETE FROM blogs WHERE slug = %s;", (slug,))
            conn.commit()
        return {"status": "success", "message": "Blog deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Bills Endpoints ===
@app.get("/api/bills")
async def get_bills():
    """Fetches all bills from the database."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, bill_no, customer_name, customer_contact, products, total_amount, payment_mode, billing_date FROM bills ORDER BY billing_date DESC;"
                )
                rows = cur.fetchall() or []
        bills = []
        for r in rows:
            rd = dict(r)
            rd["total_amount"] = _serialize_value(rd.get("total_amount"))
            rd["billing_date"] = _serialize_value(rd.get("billing_date"))
            bills.append(rd)
        return bills
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/addbill", status_code=status.HTTP_201_CREATED)
async def add_bill(request: BillCreateRequest):
    """Adds a new bill to the database."""
    try:
        full_bill_no = f"{request.billPrefix or ''}{request.billNoSuffix or ''}"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM bills WHERE bill_no = %s;", (full_bill_no,))
                if cur.fetchone():
                    raise HTTPException(
                        status_code=400, detail="Bill number already exists."
                    )
                total_amount = None
                if request.grandTotal is not None:
                    try:
                        total_amount = Decimal(str(request.grandTotal))
                    except Exception:
                        total_amount = None

                cur.execute(
                    """
                    INSERT INTO bills (bill_no, customer_name, customer_contact, products, total_amount, payment_mode, billing_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        full_bill_no,
                        request.customerName,
                        request.customerPhone,
                        psycopg2.extras.Json([item.dict() for item in request.items]),
                        total_amount,
                        request.paymentMode,
                        request.date,
                    ),
                )
            conn.commit()
        return {"status": "success", "message": "Bill added successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/letters", response_model=List[LetterResponse])
def get_letters():
    """Fetch all letters from DB."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, date, ref_number, issued_to, issued_by, subject, content
                    FROM letterheads
                    ORDER BY date DESC
                """
                )
                rows = cur.fetchall() or []
        return [{k: _serialize_value(v) for k, v in dict(r).items()} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/addletter", status_code=status.HTTP_201_CREATED)
async def add_letter(request: LetterCreateRequest):
    """Insert a new letter into DB."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM letterheads WHERE ref_number = %s;",
                    (request.ref_number,),
                )
                if cur.fetchone():
                    raise HTTPException(
                        status_code=400, detail="Reference number already exists."
                    )
                cur.execute(
                    """
                    INSERT INTO letterheads (date, ref_number, issued_to, issued_by, subject, content)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """,
                    (
                        request.date,
                        request.ref_number,
                        request.issued_to,
                        request.issued_by,
                        request.subject,
                        request.content,
                    ),
                )
            conn.commit()
        return {"status": "success", "message": "Letter added successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/addenquiry", status_code=status.HTTP_201_CREATED)
async def add_enquiry(request: EnquiryCreateRequest):
    """Insert a new enquiry into DB."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO enquiries
                    (name, email, phone, service, budget, timeline, idea, description, reference, consent)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        request.name,
                        request.email,
                        request.phone,
                        request.service,
                        request.budget,
                        request.timeline,
                        request.idea,
                        request.description,
                        request.reference,
                        request.consent,
                    ),
                )
            conn.commit()
        return {"status": "success", "message": "Enquiry added successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/enquiries")
async def get_enquiries():
    """Fetch all enquiries from DB."""
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, name, email, phone, service, budget, timeline, idea, description, reference, consent, submitted_at
                    FROM enquiries
                    ORDER BY submitted_at DESC
                """
                )
                enquiries = cur.fetchall()
        return {"status": "success", "data": enquiries}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/enquiries/{enquiry_id}/deny", status_code=status.HTTP_200_OK)
async def deny_enquiry(enquiry_id: int):
    """Deletes an enquiry from the database."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM enquiries WHERE id = %s;", (enquiry_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Enquiry not found.")
                cur.execute("DELETE FROM enquiries WHERE id = %s;", (enquiry_id,))
            conn.commit()
        return {"status": "success", "message": "Enquiry denied and deleted."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/enquiries/{enquiry_id}/approve", status_code=status.HTTP_201_CREATED)
async def approve_enquiry(enquiry_id: int, project_data: ProjectApproveRequest):
    """
    Approves an enquiry by creating a new client entry and deleting the original enquiry.
    This is handled as a single transaction to ensure data consistency.
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM enquiries WHERE id = %s;", (enquiry_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Enquiry not found.")
                cur.execute(
                    """
                    INSERT INTO active_clients (
                        client_name,
                        contact_email,
                        contact_phone,
                        project_title,
                        project_description,
                        delivery_date,
                        advance_token,
                        project_manager
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        project_data.name,
                        project_data.email,
                        project_data.phone,
                        project_data.project_title,
                        project_data.project_description,
                        project_data.delivery_date,
                        project_data.billing_details,
                        project_data.project_manager,
                    ),
                )
                cur.execute("DELETE FROM enquiries WHERE id = %s;", (enquiry_id,))
            conn.commit()
        return {
            "status": "success",
            "message": "Enquiry approved and converted to active client.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


# === Quick Contacts Endpoints ===
@app.post("/api/addquickcontact", status_code=status.HTTP_201_CREATED)
async def add_quickcontact(request: QuickContactCreateRequest):
    """Adds a new quick contact to the database."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO quickcontact (name, phone, subject, message)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        request.name,
                        request.phone,
                        request.subject,
                        request.message,
                    ),
                )
            conn.commit()
        return {"status": "success", "message": "Quick contact added successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/quickcontacts")
async def get_quickcontacts():
    """Fetches all quick contacts from the database."""
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, name, phone, subject, message, created_at
                    FROM quickcontact
                    ORDER BY created_at DESC
                    """
                )
                contacts = cur.fetchall()
        return {"status": "success", "data": contacts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/quickcontacts/{contact_id}/deny", status_code=status.HTTP_200_OK)
async def deny_quickcontact(contact_id: int):
    """Deletes a quick contact from the database."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM quickcontact WHERE id = %s;", (contact_id,))
                if not cur.fetchone():
                    raise HTTPException(
                        status_code=404, detail="Quick contact not found."
                    )
                cur.execute("DELETE FROM quickcontact WHERE id = %s;", (contact_id,))
            conn.commit()
        return {"status": "success", "message": "Quick contact denied and deleted."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/api/quickcontacts/{contact_id}/approve", status_code=status.HTTP_201_CREATED
)
async def approve_quickcontact(
    contact_id: int, project_data: QuickContactApproveRequest
):
    """
    Approves a quick contact by creating a new client entry and deleting the original contact.
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, phone
                    FROM quickcontact
                    WHERE id = %s
                    """,
                    (contact_id,),
                )
                contact_info = cur.fetchone()

                if not contact_info:
                    raise HTTPException(
                        status_code=404, detail="Quick contact not found."
                    )
                cur.execute(
                    """
                    INSERT INTO active_clients (
                        client_name,
                        contact_phone,
                        project_title,
                        project_description,
                        delivery_date,
                        advance_token,
                        project_manager,
                        project_reference
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        contact_info["name"],
                        contact_info["phone"],
                        project_data.project_title,
                        project_data.project_description,
                        project_data.delivery_date,
                        project_data.billing_details,
                        project_data.project_manager,
                        project_data.reference,
                    ),
                )
                cur.execute("DELETE FROM quickcontact WHERE id = %s;", (contact_id,))
            conn.commit()
        return {
            "status": "success",
            "message": "Quick contact approved and converted to active client.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


# === Active Clients Endpoints ===
@app.get("/api/activeclients", response_model=ActiveClientsResponse)
async def get_active_clients():
    """Fetches all active clients from the database."""
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, client_name, contact_phone, project_title, project_description,
                           delivery_date, advance_token, project_manager, project_reference,
                           status, created_at, updated_at
                    FROM active_clients
                    WHERE status = 'active'
                    ORDER BY created_at DESC
                    """
                )
                clients = cur.fetchall()
        return {"status": "success", "data": clients}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.post("/api/activeclients/{client_id}/complete", response_model=MessageResponse)
async def complete_client_project(client_id: int):
    """Marks a client project as completed."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM active_clients WHERE id = %s;", (client_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Client not found.")
                cur.execute(
                    "UPDATE active_clients SET status='completed' WHERE id = %s;",
                    (client_id,),
                )
            conn.commit()
        return {"status": "success", "message": "Client project marked as completed."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/api/admin/stats", response_model=AdminStatsResponse)
async def get_admin_stats():
    """
    Fetches key statistics for the admin dashboard.
    Returns:
        JSON with counts for new enquiries, active projects, and completed projects.
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                # Use a single query for efficiency and reliability.
                # The AS aliases are crucial to create named columns for RealDictCursor.
                cur.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM enquiries WHERE date(submitted_at) = CURRENT_DATE) AS new_enquiries_count,
                        (SELECT COUNT(*) FROM quickcontact WHERE date(created_at) = CURRENT_DATE) AS quick_contact_count,
                        (SELECT COUNT(*) FROM active_clients WHERE status = 'active') AS active_projects_count,
                        (SELECT COUNT(*) FROM active_clients WHERE status = 'completed') AS completed_projects_count;
                    """
                )

                # Fetch the single row of results as a dictionary
                counts_dict = cur.fetchone()

                if counts_dict:
                    # Access dictionary values by their column aliases
                    enquiry_count = counts_dict["new_enquiries_count"]
                    quick_contact_count = counts_dict["quick_contact_count"]
                    active_projects_count = counts_dict["active_projects_count"]
                    completed_projects_count = counts_dict["completed_projects_count"]

                    total_new_enquiries = enquiry_count + quick_contact_count

                    return {
                        "new_enquiries": total_new_enquiries,
                        "active_projects": active_projects_count,
                        "completed_projects": completed_projects_count,
                    }
                else:
                    # This case should not happen with COUNT(*) but is a good fallback
                    return {
                        "new_enquiries": 0,
                        "active_projects": 0,
                        "completed_projects": 0,
                    }

    except Exception as e:
        # A more informative error message for debugging
        raise HTTPException(
            status_code=500,
            detail=f"Database error while fetching stats: {str(e)}. "
            f"Please verify table and column names in the database.",
        )
