from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime
import uuid
import base64

from ...database import get_db
from ...models import User, Agent
from ...schemas import UserToken, AgentToken, UserLogin, AgentLogin
from ...auth import verify_password, create_user_token, create_agent_token, get_agent_by_signature

router = APIRouter()

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