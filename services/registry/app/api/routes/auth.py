from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime
from pydantic import BaseModel, EmailStr
import uuid
import base64

from ...database import get_db
from ...models import User, Agent, Wallet, WalletOwnerType
from ...schemas import UserToken, AgentToken, UserLogin, AgentLogin
from ...auth import verify_password, create_user_token, create_agent_token, get_agent_by_signature, get_password_hash

router = APIRouter()


# Registration schemas
class UserRegister(BaseModel):
    email: EmailStr
    password: str
    phone: str | None = None


class UserRegisterResponse(BaseModel):
    id: str
    email: str
    message: str


@router.post("/user/register", response_model=UserRegisterResponse, status_code=status.HTTP_201_CREATED)
async def user_register(
    user_data: UserRegister,
    db: Session = Depends(get_db)
):
    """Register a new user."""
    # Check if user exists
    existing = db.query(User).filter(User.email == user_data.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Create user
    user = User(
        id=uuid.uuid4(),
        email=user_data.email,
        password_hash=get_password_hash(user_data.password),
        phone=user_data.phone
    )
    db.add(user)
    db.flush()

    # Create user wallet
    wallet = Wallet(
        id=uuid.uuid4(),
        owner_type=WalletOwnerType.USER,
        owner_id=user.id,
        balance_credits=0,
        balance_usdc=0,
        reserved_credits=0,
        reserved_usdc=0
    )
    db.add(wallet)
    db.commit()

    return UserRegisterResponse(
        id=str(user.id),
        email=user.email,
        message="User registered successfully"
    )


@router.post("/user/login", response_model=UserToken)
async def user_login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    """Login endpoint for users."""
    # Get the user by email
    user = db.query(User).filter(User.email == form_data.username).first()

    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create a token
    token = create_user_token(user.id)

    return token


@router.post("/agent/login", response_model=AgentToken)
async def agent_login(
    login_data: AgentLogin,
    db: Session = Depends(get_db)
):
    """Login endpoint for agents."""
    # Verify the agent's signature
    agent = get_agent_by_signature(
        str(login_data.agent_id),
        login_data.signature,
        login_data.timestamp,
        db
    )

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent ID or signature",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create a token
    token = create_agent_token(agent.id)

    return token
