"""/api/wealth: household finances (manual assets, loans, goals, insurance, household profile) and the /wealth view.

Privacy: the user's own entries, local database only, never sent to an LLM (the household profile is excluded from
the advisor prompt). Unsafe routes sit behind the app's CSRF/Origin guard. Everything is computed in Python by
finresearch.wealth; no network calls.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from finresearch.db import session_scope

AssetKind = Literal["fd", "rd", "epf", "ppf", "nps", "gold", "sgb", "real_estate", "cash", "other"]
LoanKind = Literal["home", "car", "personal", "education", "other"]
AssetClass = Literal["Equity", "Debt", "Gold", "Cash", "Real estate", "Other"]


def _strip(v: str | None) -> str | None:
    v = " ".join((v or "").split())
    return v or None


class AssetIn(BaseModel):
    kind: AssetKind
    name: str = Field(min_length=1, max_length=120)
    institution: str | None = Field(None, max_length=120)
    asset_class: AssetClass | None = None
    principal: Decimal | None = Field(None, ge=0)
    rate_pct: Decimal | None = Field(None, ge=0, le=30)
    compounding: Literal[0, 1, 2, 4, 12] = 4
    monthly_contribution: Decimal | None = Field(None, ge=0)
    start_date: date | None = None
    maturity_date: date | None = None
    liquid: bool = False
    equity_pct: Decimal | None = Field(None, ge=0, le=100)
    notes: str | None = Field(None, max_length=2000)
    # a dated value (balance, statement value, estimate); stored as a valuation row
    value: Decimal | None = Field(None, ge=0)
    value_date: date | None = None

    _s = field_validator("name", "institution", mode="after")(lambda v: _strip(v))  # type: ignore[arg-type]

    @model_validator(mode="after")
    def _check(self) -> AssetIn:
        if self.kind == "fd" and (self.principal is None or self.rate_pct is None or self.start_date is None):
            raise ValueError("a fixed deposit needs principal, rate and start date")
        if self.kind == "rd" and (
            self.monthly_contribution is None or self.rate_pct is None or self.start_date is None
        ):
            raise ValueError("a recurring deposit needs the monthly instalment, rate and start date")
        if self.maturity_date and self.start_date and self.maturity_date <= self.start_date:
            raise ValueError("maturity must be after the start date")
        if self.kind not in ("fd", "rd") and self.value is None and self.principal is None:
            raise ValueError("enter the current value (or balance)")
        return self


class ValuationIn(BaseModel):
    day: date
    value: Decimal = Field(ge=0)


class LoanIn(BaseModel):
    kind: LoanKind
    name: str = Field(min_length=1, max_length=120)
    lender: str | None = Field(None, max_length=120)
    principal: Decimal = Field(gt=0)
    rate_pct: Decimal = Field(ge=0, le=40)
    tenure_months: int = Field(ge=1, le=480)
    emi: Decimal | None = Field(None, gt=0)
    start_date: date
    outstanding: Decimal | None = Field(None, ge=0)
    outstanding_as_of: date | None = None
    floating: bool = True
    notes: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _check(self) -> LoanIn:
        if (self.outstanding is None) != (self.outstanding_as_of is None):
            raise ValueError("a statement balance needs its date (and the other way round)")
        if self.emi is not None and self.emi <= self.principal * self.rate_pct / 1200:
            raise ValueError("the EMI does not cover a month's interest")
        return self


class GoalIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_inr: Decimal = Field(gt=0)
    target_date: date
    priority: Literal["high", "medium", "low"] = "medium"
    inflation_pct: Decimal = Field(Decimal(6), ge=0, le=20)
    current_inr: Decimal = Field(Decimal(0), ge=0)
    monthly_sip: Decimal = Field(Decimal(0), ge=0)
    step_up_pct: Decimal = Field(Decimal(0), ge=0, le=50)
    linked_asset_ids: list[int] = Field(default_factory=list, max_length=50)
    portfolio_pct: Decimal = Field(Decimal(0), ge=0, le=100)
    equity_pct: Decimal | None = Field(None, ge=0, le=100)
    gold_pct: Decimal = Field(Decimal(0), ge=0, le=100)
    in_cover: bool = False  # count the gap in the term-cover need (a goal the family would still need)
    notes: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _check(self) -> GoalIn:
        if self.equity_pct is not None and self.equity_pct + self.gold_pct > 100:
            raise ValueError("equity + gold cannot exceed 100 %")
        return self


class PolicyIn(BaseModel):
    kind: Literal["term", "health", "other"]
    name: str = Field(min_length=1, max_length=120)
    cover_inr: Decimal = Field(gt=0)
    premium_inr: Decimal | None = Field(None, ge=0)
    end_date: date | None = None
    employer: bool = False
    notes: str | None = Field(None, max_length=2000)


def add_wealth_routes(app: FastAPI, *, clock: Callable[[], datetime] | None = None) -> None:
    from finresearch.fincalc.dates import IST, today_ist

    def today() -> date:
        override = getattr(app.state, "wealth_today", None)
        if override is not None:
            return override
        return clock().astimezone(IST).date() if clock else today_ist()

    def _get(s, model, id_: int):
        row = s.get(model, id_)
        if row is None:
            raise HTTPException(404, f"unknown {model.__tablename__.split('_', 1)[1]} {id_}")
        return row

    @app.get("/api/wealth")
    def wealth() -> dict[str, Any]:
        """Net worth (latest portfolio snapshot + manual assets − loans) and its month-end history, allocation over
        the balance sheet vs the glide-path rule of thumb, emergency fund with the DICGC per-bank flag, needs-based
        term cover, EMI/income and prepay-vs-invest per loan."""
        from finresearch.wealth.service import overview

        with session_scope() as s:
            return overview(s, today())

    @app.get("/api/wealth/goals/{goal_id}/plan")
    def goal_plan(
        goal_id: int,
        seed: int | None = Query(None, ge=0, lt=2**32),
        n: int | None = Query(None, ge=500, le=20000),
    ) -> dict[str, Any]:
        """Seeded Monte Carlo for one goal: P(success) with its range, fan chart, SIP needed for 75 % / 90 %."""
        from finresearch.wealth.service import goal_plan as run

        with session_scope() as s:
            try:
                return run(s, goal_id, today(), seed=seed, n=n)
            except LookupError as e:
                raise HTTPException(404, f"unknown goal {goal_id}") from e
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

    # ------------------------------------------------------------------ household profile and assumptions
    @app.get("/api/wealth/household")
    def get_household() -> dict[str, Any]:
        from finresearch.suggest.advisor import load_profile

        with session_scope() as s:
            return load_profile(s).household.model_dump(mode="json")

    @app.put("/api/wealth/household")
    def put_household(body: dict[str, Any]) -> dict[str, Any]:
        """Replace the household block of the profile (the rest of the profile is untouched)."""
        from pydantic import ValidationError

        from finresearch.suggest.advisor import load_profile, save_profile
        from finresearch.suggest.profile import Household

        try:
            hh = Household.model_validate(body)
        except ValidationError as e:
            raise HTTPException(
                422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            ) from e
        with session_scope() as s:
            prof = load_profile(s)
            prof.household = hh
            save_profile(s, prof)
            return hh.model_dump(mode="json")

    @app.get("/api/wealth/assumptions")
    def get_assumptions() -> dict[str, Any]:
        from finresearch.wealth.service import get_assumptions as ga

        with session_scope() as s:
            return ga(s)

    @app.put("/api/wealth/assumptions")
    def put_assumptions(body: dict[str, Any]) -> dict[str, Any]:
        """Return/volatility per class (equity, debt, gold), inflation, seed and path count. {} restores defaults."""
        from finresearch.wealth.service import set_assumptions

        with session_scope() as s:
            try:
                return set_assumptions(s, body)
            except (ValueError, KeyError, TypeError) as e:
                raise HTTPException(422, str(e) or "invalid assumptions") from e

    # ------------------------------------------------------------------ assets
    def _apply_asset(a, body: AssetIn) -> None:
        for k, v in body.model_dump(exclude={"value", "value_date"}).items():
            setattr(a, k, v)

    def _add_value(s, asset_id: int, day: date, value: Decimal) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from finresearch.db.models import WealthValuation

        s.execute(
            insert(WealthValuation)
            .values(asset_id=asset_id, day=day, value=value)
            .on_conflict_do_update(index_elements=["asset_id", "day"], set_={"value": value})
        )

    @app.post("/api/wealth/assets")
    def add_asset(body: AssetIn) -> dict[str, Any]:
        from finresearch.db.models import WealthAsset
        from finresearch.wealth.service import asset_json

        with session_scope() as s:
            a = WealthAsset()
            _apply_asset(a, body)
            s.add(a)
            s.flush()
            if body.value is not None:
                _add_value(s, a.id, body.value_date or today(), body.value)
            return asset_json(a)

    @app.put("/api/wealth/assets/{asset_id}")
    def put_asset(asset_id: int, body: AssetIn) -> dict[str, Any]:
        from finresearch.db.models import WealthAsset
        from finresearch.wealth.service import asset_json

        with session_scope() as s:
            a = _get(s, WealthAsset, asset_id)
            _apply_asset(a, body)
            a.updated_at = datetime.now().astimezone()
            if body.value is not None:
                _add_value(s, a.id, body.value_date or today(), body.value)
            return asset_json(a)

    @app.post("/api/wealth/assets/{asset_id}/values")
    def add_value(asset_id: int, body: ValuationIn) -> dict[str, Any]:
        """Record a dated value (the latest on or before a day values the asset that day)."""
        from finresearch.db.models import WealthAsset

        with session_scope() as s:
            _get(s, WealthAsset, asset_id)
            _add_value(s, asset_id, body.day, body.value)
            return {"asset_id": asset_id, "day": body.day.isoformat(), "value": float(body.value)}

    @app.delete("/api/wealth/assets/{asset_id}/values/{day}")
    def delete_value(asset_id: int, day: date) -> dict[str, Any]:
        from sqlalchemy import delete

        from finresearch.db.models import WealthValuation

        with session_scope() as s:
            n = s.execute(
                delete(WealthValuation).where(
                    WealthValuation.asset_id == asset_id, WealthValuation.day == day
                )
            ).rowcount
            if not n:
                raise HTTPException(404, "no value on that day")
            return {"deleted": n}

    @app.delete("/api/wealth/assets/{asset_id}")
    def delete_asset(asset_id: int) -> dict[str, Any]:
        from sqlalchemy import select

        from finresearch.db.models import WealthAsset, WealthGoal

        with session_scope() as s:
            s.delete(_get(s, WealthAsset, asset_id))
            for g in s.scalars(select(WealthGoal)):  # unlink from goals
                if asset_id in (g.linked_asset_ids or []):
                    g.linked_asset_ids = [i for i in g.linked_asset_ids if i != asset_id]
            return {"deleted": asset_id}

    # ------------------------------------------------------------------ loans, goals, policies (plain CRUD)
    def _crud(
        path: str,
        model_name: str,
        body_model: type[BaseModel],
        to_json_name: str,
        check: Callable[[Any, BaseModel], None] | None = None,
    ) -> None:
        import finresearch.db.models as models
        import finresearch.wealth.service as svc

        model = getattr(models, model_name)
        to_json = getattr(svc, to_json_name)

        def create(body: BaseModel) -> dict[str, Any]:
            with session_scope() as s:
                if check:
                    check(s, body)
                row = model(**body.model_dump())  # type: ignore[attr-defined]
                s.add(row)
                s.flush()
                return to_json(row)

        def update(item_id: int, body: BaseModel) -> dict[str, Any]:
            with session_scope() as s:
                row = _get(s, model, item_id)
                if check:
                    check(s, body)
                for k, v in body.model_dump().items():  # type: ignore[attr-defined]
                    setattr(row, k, v)
                return to_json(row)

        def remove(item_id: int) -> dict[str, Any]:
            with session_scope() as s:
                s.delete(_get(s, model, item_id))
                return {"deleted": item_id}

        # the closures' `body` annotation is a string under postponed evaluation: give FastAPI the real model
        create.__annotations__["body"] = body_model
        update.__annotations__["body"] = body_model
        app.post(f"/api/wealth/{path}")(create)
        app.put(f"/api/wealth/{path}/{{item_id}}")(update)
        app.delete(f"/api/wealth/{path}/{{item_id}}")(remove)

    def _goal_links(s, body: GoalIn) -> None:
        from finresearch.db.models import WealthAsset

        missing = [i for i in body.linked_asset_ids if s.get(WealthAsset, i) is None]
        if missing:
            raise HTTPException(422, f"unknown linked assets: {missing}")
        body.linked_asset_ids = list(dict.fromkeys(body.linked_asset_ids))

    _crud("loans", "WealthLoan", LoanIn, "loan_json")
    _crud("goals", "WealthGoal", GoalIn, "goal_json", check=_goal_links)
    _crud("policies", "WealthPolicy", PolicyIn, "policy_json")
