"""Conservative phrase rules. User text never supplies a callable tool or SQL."""

import re
from typing import Any

import sqlalchemy as sa

from app.database import schema as db
from app.routing.models import Intent, Issuer, Period, QueryPlan, QueryRequest, Step
from app.services.calculations import FORMULAS
from app.services.financial_query import QUERY_CONCEPTS

PHRASES = {
    "revenue": ("revenue", "revenues", "sales"),
    "net_income": ("net income", "net profit"),
    "operating_income": ("operating income", "operating profit"),
    "gross_profit": ("gross profit",),
    "operating_cash_flow": ("operating cash flow", "cash flow from operations"),
    "capital_expenditures": ("capital expenditures", "capex"),
    "current_assets": ("current assets",),
    "current_liabilities": ("current liabilities",),
    "assets": ("total assets", "assets"),
    "liabilities": ("total liabilities", "liabilities"),
    "equity": ("shareholders equity", "stockholders equity", "equity"),
    "cash": ("cash balance",),
    "gross_margin": ("gross margin",),
    "operating_margin": ("operating margin",),
    "net_margin": ("net margin", "net profit margin"),
    "fcf_growth": ("free cash flow growth", "fcf growth"),
    "fcf": ("free cash flow", "fcf"),
    "current_ratio": ("current ratio",),
    "debt_equity": (
        "debt to equity",
        "debt equity",
    ),
    "roe": ("return on equity", "roe"),
    "roa": ("return on assets", "roa"),
    "ebitda": ("ebitda",),
    "cagr": ("cagr", "compound annual growth rate"),
    "yoy": ("year over year growth", "year over year", "yoy growth", "yoy"),
    "growth": ("growth",),
    "discrete_quarter": ("discrete quarter",),
}


def normalized(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def contains(text: str, phrase: str) -> bool:
    return f" {normalized(phrase)} " in f" {normalized(text)} "


def load_issuers(connection: sa.Connection) -> list[Issuer]:
    rows = connection.execute(
        sa.select(db.securities.c.ticker, db.companies.c.id, db.companies.c.legal_name).join(
            db.companies
        )
    ).mappings()
    output = []
    for row in rows:
        name = normalized(row["legal_name"])
        short = re.sub(r"\b(inc|incorporated|corp|corporation|company|co|ltd|plc|com)\b", "", name)
        short = " ".join(short.split())
        aliases = [row["ticker"], name]
        if len(short) >= 3:
            aliases.append(short)
        output.append(
            Issuer(
                ticker=row["ticker"],
                company_id=str(row["id"]),
                legal_name=row["legal_name"],
                aliases=aliases,
            )
        )
    return output


def plan_query(request: QueryRequest, registry: list[Issuer]) -> QueryPlan:
    text = normalized(request.question)
    issues: list[str] = []
    currencies = {
        "USD": ("usd", "us dollars"),
        "EUR": ("eur", "euros"),
        "GBP": ("gbp", "pounds"),
        "JPY": ("jpy", "yen"),
        "INR": ("inr", "rupees"),
    }
    if any(
        code != request.unit and any(contains(text, word) for word in words)
        for code, words in currencies.items()
    ):
        issues.append(
            "The question currency conflicts with the explicit unit; specify the intended unit."
        )
    if re.search(r"\b(not|except|instead)\b", text):
        issues.append(
            "Restate the requested metrics directly; negated routing instructions are ambiguous."
        )
    by_ticker = {i.ticker: i for i in registry}
    explicit = set(request.tickers)
    mentioned = {i.ticker for i in registry if any(contains(text, a) for a in i.aliases)}
    if explicit:
        if explicit - by_ticker.keys():
            issues.append("Unknown ticker: provide an imported issuer ticker.")
        if mentioned - explicit:
            issues.append("The question and explicit ticker scope disagree.")
        selected = [by_ticker[t] for t in request.tickers if t in by_ticker]
    else:
        selected = [i for i in registry if i.ticker in mentioned]
    if not selected:
        issues.append("Specify the issuer ticker or its imported legal name.")
    if len(selected) > 4:
        issues.append("Limit the query to four issuers.")
        selected = selected[:4]
    if len({i.company_id for i in selected}) != len(selected):
        issues.append(
            "Multiple securities resolve to the same issuer; specify one ticker per issuer."
        )
    if request.issuer_periods.keys() - {i.ticker for i in selected}:
        issues.append("Per-issuer periods must belong to the selected ticker scope.")
    comparison = len(selected) > 1 or bool(re.search(r"\b(compare|versus|vs|between)\b", text))
    if comparison and len(selected) < 2:
        issues.append("A company comparison requires at least two resolved issuers.")
    remaining = text
    metrics = []
    for phrase, metric in sorted(
        ((p, m) for m, phrases in PHRASES.items() for p in phrases), key=lambda item: -len(item[0])
    ):
        pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
        if re.search(pattern, remaining):
            if metric not in metrics:
                metrics.append(metric)
            remaining = re.sub(pattern, " ", remaining)
    facts = [m for m in metrics if m in QUERY_CONCEPTS]
    generic = [m for m in metrics if m in {"growth", "yoy", "cagr", "discrete_quarter"}]
    if generic and len(facts) != 1:
        issues.append(
            "Specify exactly one reported metric for growth/CAGR/derived-quarter requests."
        )
    input_metric = facts[0] if len(facts) == 1 else "revenue"
    if generic:
        metrics = [m for m in metrics if m not in facts]
    if re.search(r"\b(dividend|dividends|eps|p e|valuation|profit margin)\b", remaining):
        issues.append(
            "The requested metric is unsupported or ambiguous; specify an available metric."
        )
    market = bool(re.search(r"\b(stock price|share price|quote|trading volume|market cap)\b", text))
    risk = bool(re.search(r"\b(risk score|probability|predict|prediction|bankruptcy)\b", text))
    document = bool(
        re.search(r"\b(explain|why|filing|filings|disclose|disclosed|risks|risk factors)\b", text)
    )
    company = bool(
        re.search(
            r"\b(overview|company information|what does|business description|headquarters|ceo)\b",
            text,
        )
    )
    period = request.period
    # Calendar years never establish fiscal period endpoints. A single exact date can.
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", request.question)
    if re.search(r"\bas of\b", text) and request.as_of is None:
        issues.append("Supply an explicit timezone-aware as-of cutoff.")
    if period is None and metrics and len(dates) == 1 and "as of" not in text:
        basis: Any = "annual" if "annual" in text else ("quarter" if "quarterly" in text else None)
        if basis:
            try:
                period = Period(basis=basis, period_end=dates[0])
            except ValueError:
                issues.append("Provide a valid period end date.")
    scopes = list(request.issuer_periods.values()) + ([period] if period else [])
    allowed_dates = {
        str(d) for scope in scopes for d in (scope.period_end, scope.comparison_end) if d
    }
    if request.as_of:
        allowed_dates.add(str(request.as_of.date()))
    if metrics and scopes and set(dates) - allowed_dates:
        issues.append(
            "Question dates conflict with the explicit reporting/comparison/cutoff dates."
        )
    if len(dates) > 1 and not request.period and not request.issuer_periods:
        issues.append("Assign multiple dates explicitly to reporting and comparison periods.")
    cutoff_match = re.search(r"\bas\s+of\s+(\d{4}-\d{2}-\d{2})", request.question, re.I)
    if cutoff_match and request.as_of and cutoff_match[1] != str(request.as_of.date()):
        issues.append("The question cutoff conflicts with the explicit as-of timestamp.")
    if (
        re.search(r"\b(latest|today|current)\b", text)
        and metrics
        and not period
        and not request.issuer_periods
    ):
        issues.append("Specify an exact reporting period; latest financial facts are not inferred.")
    if (document or company) and request.form is None:
        issues.append("Specify a filing form for document evidence (for example 10-K).")
    if (
        (document or company)
        and request.accession is None
        and re.search(r"\b(last year|last quarter|previous|historical|earlier)\b", text)
    ):
        issues.append("Historical filing requests require an explicit accession.")
    if (document or company) and metrics and request.accession is None:
        issues.append(
            "Combined metric explanations require an explicit accession for period-scoped evidence."
        )
    if (
        (document or company)
        and re.search(r"\b(?:19|20)\d{2}\b", text)
        and request.accession is None
    ):
        issues.append("Historical document questions require an explicit accession.")
    if request.accession and len(selected) != 1:
        issues.append("An explicit accession must be scoped to one issuer.")
    steps = []
    for issuer in selected:
        scope = request.issuer_periods.get(issuer.ticker, period)
        for metric in metrics:
            if scope is None:
                issues.append(f"Specify reporting basis and period end for {issuer.ticker}.")
            balances = {
                "assets",
                "liabilities",
                "equity",
                "cash",
                "current_assets",
                "current_liabilities",
                "current_ratio",
                "debt_equity",
            }
            if scope and not generic and (metric in balances) != (scope.basis == "instant"):
                issues.append(
                    f"Specify {'instant' if metric in balances else 'duration'} basis for {metric}."
                )
            if scope and (
                metric in {"growth", "yoy", "cagr", "fcf_growth", "discrete_quarter"}
            ) != (scope.comparison_end is not None):
                issues.append(
                    f"Comparison date is required only for comparative calculations ({metric})."
                )
            steps.append(
                Step(
                    intent=Intent.CALCULATION if metric in FORMULAS else Intent.FACT,
                    ticker=issuer.ticker,
                    metric=metric,
                    input_metric=input_metric,
                    period=scope,
                )
            )
        for active, intent in [
            (document, Intent.DOCUMENT),
            (company and not document, Intent.COMPANY),
            (market, Intent.MARKET),
            (risk, Intent.RISK),
        ]:
            if active:
                steps.append(Step(intent=intent, ticker=issuer.ticker))
    if not metrics and not any([document, company, market, risk]):
        issues.append(
            "Specify a supported metric, filing question, market request, or risk request."
        )
    if comparison and not metrics:
        issues.append("Specify financial metrics and periods to align a company comparison.")
    if len(steps) > 24:
        issues.append("Split this request into fewer metrics or issuers.")
    intents = list(dict.fromkeys(s.intent for s in steps))
    if comparison:
        intents.append(Intent.COMPARISON)
    return QueryPlan(
        request=request,
        status="NEEDS_CLARIFICATION" if issues else "READY",
        issuers=selected,
        intents=intents,
        steps=steps[:24],
        clarifications=list(dict.fromkeys(issues)),
    )
