"""Dados de leads e validação independente de serviços externos."""
import re
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StringConstraints, UUID4, field_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Signal = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Stage = Literal['Awareness', 'Consideration', 'Decision']
Source = Literal['clay', 'phantom', 'gtm-ai', 'manual']


class Firmographics(BaseModel):
    """Perfil declarado pela fonte; vazio significa desconhecido."""
    industry: Text = ''
    company_size: Text = Field(default='', validation_alias=AliasChoices('company_size', 'companySize'))
    location: Text = ''
    revenue: Text = ''


class IntentData(BaseModel):
    """Sinais observados; uma atividade desconhecida não recebe data inventada."""
    score: int = Field(default=0, ge=0, le=100, strict=True)
    signals: list[Signal] = Field(default_factory=list, max_length=100)
    stage: Stage = 'Awareness'
    last_activity: datetime | None = Field(default=None, validation_alias=AliasChoices('last_activity', 'lastActivity'))

    @field_validator('last_activity')
    @classmethod
    def activity_timezone(cls, value: datetime | None) -> datetime | None:
        """Normaliza datas sem fuso como UTC."""
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


class Lead(BaseModel):
    """Lead validado, com identificador interno e campos em snake_case."""
    model_config = ConfigDict(str_strip_whitespace=True)
    id: UUID4 = Field(default_factory=uuid4)
    name: Text = Field(min_length=1)
    # EmailStr exige email-validator, ausente das dependências do projeto.
    email: str = Field(min_length=3, max_length=254)
    company: Text = Field(min_length=1)
    role: Text = ''
    firmographics: Firmographics = Field(default_factory=Firmographics)
    technographics: list[Text] = Field(default_factory=list, max_length=100)
    intent: IntentData = Field(default_factory=IntentData)
    source: Source = 'manual'
    is_favorite: bool = Field(default=False, validation_alias=AliasChoices('is_favorite', 'favorite'))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), validation_alias=AliasChoices('created_at', 'createdAt'))
    opt_out: bool = Field(default=False, exclude=True, validation_alias=AliasChoices('opt_out', 'optOut'))

    @field_validator('email')
    @classmethod
    def validate_email(cls, value: str) -> str:
        """Aplica a regra de endereço já adotada pelas contas do portal."""
        value = value.strip().casefold()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Informe um e-mail válido.')
        return value
