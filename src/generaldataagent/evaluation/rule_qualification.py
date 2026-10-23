"""Synthetic rule qualification with an independent raw-record/Fraction oracle.

No public benchmark questions, labels or payment assets are read. This bounded
suite qualifies facet matching and arithmetic, not natural-language reasoning.
"""

import hashlib
import json
import random
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime

DOCUMENTS = (
    "A null predicate accepts all values.\n"
    "Empty lists accept no values unless empty_list_is_wildcard is explicitly enabled.\n"
    "fee = fixed_amount + rate * transaction_value / 10000"
)
EXCERPT = "A null predicate accepts all values."


def synthetic_cases(seed=91307, count=48):
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("qualification case count must be between 1 and 1000")
    rng = random.Random(seed)
    cases = []
    for index in range(count):
        annex = []
        for number in range(8):
            annex.append(
                dict(
                    ID=f"rule-{number}",
                    card_scheme=rng.choice(["Alpha", "Beta"]),
                    account_type=rng.choice([None, [], ["retail"], ["digital"]]),
                    merchant_category_code=rng.choice([None, [], [1234], [5678]]),
                    aci=rng.choice([None, [], ["A"], ["B"]]),
                    is_credit=rng.choice([None, False, True]),
                    intracountry=rng.choice([None, False, True]),
                    fixed_amount=f"0.{rng.randrange(100):02d}",
                    rate=str(rng.randrange(1, 60)),
                )
            )
        # Alternate unrestricted, individual and joint facet constraints.
        constraints = [
            {},
            {"account_type": "retail"},
            {"aci": "A"},
            {"is_credit": True, "intracountry": False},
            {"card_scheme": "Beta", "merchant_category_code": 1234},
            {"account_type": "digital", "aci": "B", "card_scheme": "Alpha"},
        ][index % 6]
        request = dict(
            operation="average_fee" if index % 2 else "select",
            semantics_excerpt=EXCERPT,
            empty_list_is_wildcard=bool(index % 2),
            transaction_value=f"{rng.randrange(1000)}.{rng.randrange(100):02d}",
            **constraints,
        )
        cases.append(dict(case_id=f"synthetic-{index:03d}", annex=annex, request=request))
    return cases


def independent_expected(annex, request):
    """Interpret raw facets directly; never call the engine or its parser."""
    selected = []
    list_fields = ("account_type", "merchant_category_code", "aci")
    for row in annex:
        accepted = True
        for field in (*list_fields, "card_scheme", "is_credit", "intracountry"):
            observed = request.get(field)
            if observed is None:
                continue
            predicate = row.get(field)
            if predicate is None:
                continue
            if field in list_fields:
                accepts = (
                    request["empty_list_is_wildcard"] if predicate == [] else observed in predicate
                )
            else:
                accepts = predicate == observed
            if not accepts:
                accepted = False
                break
        if accepted:
            selected.append(row)
    fees = [
        Fraction(row["fixed_amount"])
        + Fraction(row["rate"]) * Fraction(request["transaction_value"]) / 10000
        for row in selected
    ]
    mean = sum(fees, Fraction(0)) / len(fees) if fees else None
    return [row["ID"] for row in selected], mean


# --- merchant/month context suite -------------------------------------------

CONTEXT_SUITE = "synthetic-rule-context-qualification-v1"


def _calendar(day_of_year, year=2023):
    from datetime import date, timedelta

    return date(year, 1, 1) + timedelta(days=day_of_year - 1)


def _band_accepts(text, observation):
    """Independent band reading for the ``>``/``>=``/``<``/``<=``/range forms used here."""
    if text is None:
        return True
    text = text.strip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    for operator in (">=", "<=", ">", "<"):
        if text.startswith(operator):
            bound = Fraction(text[len(operator) :]) / (100 if percent else 1)
            if operator == ">":
                return observation > bound
            if operator == ">=":
                return observation >= bound
            if operator == "<":
                return observation < bound
            return observation <= bound
    low, _, high = text.partition("-")
    scale = 100 if percent else 1
    low_value = Fraction(low) / scale
    high_value = Fraction(high) / scale if high else low_value
    return low_value <= observation <= high_value


def _row_facets(row):
    return dict(
        card_scheme=row["card_scheme"],
        aci=row["aci"],
        is_credit=row["is_credit"] in (True, 1, "True", "true", "1"),
        intracountry=row["issuing_country"] == row["acquirer_country"],
        amount=Fraction(str(row["eur_amount"])),
    )


def context_cases(seed=91307, count=24):
    """Synthetic merchants, payments and rule annexes for applicable/counterfactual."""
    if type(count) is not int or not 1 <= count <= 500:
        raise ValueError("context case count must be between 1 and 500")
    rng = random.Random(seed + 7)
    cases = []
    for index in range(count):
        payments = []
        for _ in range(rng.randrange(4, 12)):
            day = rng.randrange(1, 120)
            payments.append(
                dict(
                    merchant="Shop",
                    year=2023,
                    day_of_year=day,
                    eur_amount=rng.randrange(10, 500),
                    has_fraudulent_dispute=rng.choice([True, False, False, False]),
                    card_scheme=rng.choice(["Alpha", "Beta"]),
                    aci=rng.choice(["A", "B"]),
                    is_credit=rng.choice([True, False]),
                    issuing_country="NL",
                    acquirer_country=rng.choice(["NL", "BE"]),
                )
            )
        merchant = dict(
            merchant="Shop",
            account_type=rng.choice(["R", "D"]),
            merchant_category_code=rng.choice([5411, 7995]),
            capture_delay=rng.choice(["1", "immediate", "manual"]),
        )
        annex = []
        for number in range(6):
            # Every fourth rule is deliberately unconstrained so the suite
            # exercises the counterfactual success path, not only refusals.
            unconstrained = number == 0
            annex.append(
                dict(
                    ID=f"ctx-{number}",
                    card_scheme=rng.choice(["Alpha", "Beta"]),
                    account_type=None if unconstrained else rng.choice([None, ["R"], ["D"]]),
                    merchant_category_code=(
                        None if unconstrained else rng.choice([None, [5411], [7995]])
                    ),
                    capture_delay=(
                        None if unconstrained else rng.choice([None, "1", "immediate", "manual"])
                    ),
                    monthly_volume=(
                        None if unconstrained else rng.choice([None, ">300", ">5000", "1000-2000"])
                    ),
                    monthly_fraud_level=(
                        None if unconstrained else rng.choice([None, ">10%", ">40%"])
                    ),
                    aci=None if unconstrained else rng.choice([None, ["A"], ["B"]]),
                    is_credit=None if unconstrained else rng.choice([None, True, False]),
                    intracountry=None if unconstrained else rng.choice([None, True, False]),
                    fixed_amount=f"0.{rng.randrange(100):02d}",
                    rate=str(rng.randrange(1, 60)),
                )
            )
        # Choose the time scope from months the fixture actually populated, so a
        # generated case never fails for a reason the implementation controls.
        populated = sorted({_calendar(row["day_of_year"]).month for row in payments})
        target_month = rng.choice(populated)
        in_month = [row for row in payments if _calendar(row["day_of_year"]).month == target_month]
        day_scope = rng.choice([None, *sorted({row["day_of_year"] for row in in_month})])
        # Point the unconstrained rule at a scheme present in the scoped rows so
        # the counterfactual success path is genuinely exercised, not only refused.
        scoped_schemes = [
            row["card_scheme"]
            for row in in_month
            if day_scope is None or row["day_of_year"] == day_scope
        ]
        if scoped_schemes:
            annex[0]["card_scheme"] = rng.choice(scoped_schemes)
        request = dict(
            operation="applicable" if index % 2 == 0 else "counterfactual",
            semantics_excerpt=EXCERPT,
            merchant="Shop",
            year=2023,
            day_of_year=day_scope,
            month=None if day_scope is not None else target_month,
            rule_id=f"ctx-{rng.choice([0, 0, 1, 2, 3, 4, 5])}" if index % 2 else None,
            alternate_rate=str(rng.randrange(1, 60)) if index % 2 else None,
        )
        cases.append(
            dict(
                case_id=f"context-{index:03d}",
                merchant=merchant,
                payments=payments,
                annex=annex,
                request={key: value for key, value in request.items() if value is not None},
            )
        )
    return cases


def independent_context_expected(merchant, payments, annex, request):
    """Re-derive context answers from raw records and exact fractions only.

    Returns ``(ids, counterfactual_or_none)``. Nothing here imports the rule
    engine, its parser, or the workflow handlers.
    """
    year = request["year"]
    target_month = (
        _calendar(request["day_of_year"], year).month
        if request.get("day_of_year") is not None
        else request["month"]
    )
    monthly = [
        row
        for row in payments
        if row["year"] == year
        and row["merchant"] == merchant["merchant"]
        and _calendar(row["day_of_year"], year).month == target_month
    ]
    if not monthly:
        raise ValueError("context fixture produced an empty month")
    volume = sum((Fraction(str(row["eur_amount"])) for row in monthly), Fraction(0))
    fraud = sum(
        (Fraction(str(row["eur_amount"])) for row in monthly if row["has_fraudulent_dispute"]),
        Fraction(0),
    )
    ratio = fraud / volume
    scoped = [
        row
        for row in monthly
        if request.get("day_of_year") is None or row["day_of_year"] == request["day_of_year"]
    ]
    transactions = [_row_facets(row) for row in scoped]

    def accepts_list(predicate, value):
        return (
            predicate is None
            or (predicate == [] and request.get("empty_list_is_wildcard", False))
            or value in predicate
        )

    matched = []
    for rule in annex:
        if not accepts_list(rule.get("account_type"), merchant["account_type"]):
            continue
        if not accepts_list(rule.get("merchant_category_code"), merchant["merchant_category_code"]):
            continue
        if (
            rule.get("capture_delay") is not None
            and rule["capture_delay"] != merchant["capture_delay"]
        ):
            continue
        if not _band_accepts(rule.get("monthly_volume"), volume):
            continue
        if not _band_accepts(rule.get("monthly_fraud_level"), ratio):
            continue
        hits = [
            transaction
            for transaction in transactions
            if transaction["card_scheme"] == rule["card_scheme"]
            and accepts_list(rule.get("aci"), transaction["aci"])
            and (rule.get("is_credit") is None or transaction["is_credit"] == rule["is_credit"])
            and (
                rule.get("intracountry") is None
                or transaction["intracountry"] == rule["intracountry"]
            )
        ]
        if hits:
            matched.append((rule, hits))
    ids = [rule["ID"] for rule, _ in matched]

    change = None
    if request["operation"] == "counterfactual":
        targets = [(rule, hits) for rule, hits in matched if rule["ID"] == request["rule_id"]]
        if len(targets) != 1:
            return ids, "not_applicable"
        rule, hits = targets[0]
        total = sum((hit["amount"] for hit in hits), Fraction(0))
        count = len(hits)
        baseline = Fraction(rule["fixed_amount"]) * count + Fraction(rule["rate"]) * total / 10000
        alternate = (
            Fraction(rule["fixed_amount"]) * count
            + Fraction(request["alternate_rate"]) * total / 10000
        )
        change = dict(
            transaction_count=count,
            baseline_fee=baseline,
            alternate_fee=alternate,
            delta=alternate - baseline,
        )
    return ids, change


def _context_database(path, merchant, payments):
    import sqlite3

    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE merchant_data (payload TEXT)")
        db.execute("INSERT INTO merchant_data VALUES (?)", (json.dumps(merchant),))
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
                   eur_amount REAL, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
                   is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.executemany(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["merchant"],
                    row["year"],
                    row["day_of_year"],
                    row["eur_amount"],
                    "True" if row["has_fraudulent_dispute"] else "False",
                    row["card_scheme"],
                    row["aci"],
                    "True" if row["is_credit"] else "False",
                    row["issuing_country"],
                    row["acquirer_country"],
                )
                for row in payments
            ],
        )


def qualify_context(output: Path, *, seed=91307, count=24):
    """Run the merchant/month suite through the real workflow and payments source."""
    from generaldataagent.business.rules.payments import SQLitePaymentsSource

    cases = context_cases(seed, count)
    output.mkdir(parents=True, exist_ok=False)
    (output / "cases.json").write_text(json.dumps(cases, indent=2) + "\n")
    actor = ActorContext(
        user_id="qualification",
        tenant_id="synthetic",
        project_id="rules",
        data_domains=("synthetic",),
    )
    records = []
    with open_protected_checkpointer(
        output / "checkpoints.sqlite", encryption_key=b"synthetic-rule-qualification-key!"[:32]
    ) as saver:
        for case in cases:
            database = output / f"{case['case_id']}.sqlite"
            _context_database(database, case["merchant"], case["payments"])
            payments = SQLitePaymentsSource(
                database, allowed_relations={"merchant_data", "payments"}
            )
            adapter = AnnexWorkflowServices(case["annex"], documents=DOCUMENTS, payments=payments)
            flow = RuleWorkflowRuntime(
                services=adapter.services(),
                checkpointer=saver,
                source_hashes=adapter.source_hashes,
                workflow_version=CONTEXT_SUITE,
            )
            expected_ids, expected_change = independent_context_expected(
                case["merchant"], case["payments"], case["annex"], case["request"]
            )
            try:
                thread, state = flow.run(RuleAnalysisRequest.model_validate(case["request"]), actor)
            except Exception as exc:
                # A refusal the oracle also predicts is a correct outcome, not a
                # defect; anything else stays a retained failure.
                expected_refusal = (
                    expected_change == "not_applicable"
                    and type(exc) is ValueError
                    and "counterfactual_not_applicable" in str(exc)
                )
                records.append(
                    dict(
                        case_id=case["case_id"],
                        passed=expected_refusal,
                        error_type=type(exc).__name__,
                        expected_ids=expected_ids,
                        observed_ids=None,
                        source_hashes=adapter.source_hashes(),
                    )
                )
                continue
            delivery = state.get("delivery", {})
            # ``items`` is delivered by applicable only; a counterfactual
            # answer is judged solely by its recomputed fee change.
            matches = state["status"] == "completed" and (
                case["request"]["operation"] != "applicable"
                or delivery.get("items") == expected_ids
            )
            if expected_change == "not_applicable":
                change_ok = state["status"] == "failed"
            elif expected_change is None:
                change_ok = True
            else:
                # One part in 10^24 absorbs the engine's Decimal division while
                # still rejecting any semantic difference in the delta.
                change_ok = state["status"] == "completed" and all(
                    abs(Fraction(Decimal(delivery[key])) - value) <= Fraction(1, 10**24)
                    for key, value in expected_change.items()
                    if key != "transaction_count"
                )
                change_ok = change_ok and (
                    delivery.get("transaction_count") == expected_change["transaction_count"]
                )
            records.append(
                dict(
                    case_id=case["case_id"],
                    thread_id=thread,
                    passed=bool(matches and change_ok),
                    expected_ids=expected_ids,
                    observed_ids=delivery.get("items"),
                    expected_change={
                        key: str(value) for key, value in (expected_change or {}).items()
                    }
                    if expected_change != "not_applicable"
                    else "not_applicable",
                    observed_change={
                        key: delivery.get(key)
                        for key in ("transaction_count", "baseline_fee", "alternate_fee", "delta")
                    },
                    status=state["status"],
                    source_hashes=adapter.source_hashes(),
                )
            )
    report = dict(
        suite=CONTEXT_SUITE,
        seed=seed,
        n=len(records),
        passed=sum(row["passed"] for row in records),
        results=records,
        corpus_sha256=hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        model_calls=0,
        benchmark_score=False,
        production_accepted=False,
        limitations=[
            "applicable and counterfactual only",
            "synthetic structured requests and synthetic payments",
            "independent oracle re-derives the documented semantics but shares the annex",
            "no independent human review or unseen natural-language evaluation",
        ],
    )
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def qualify_rules(output: Path, *, seed=91307, count=48):
    cases = synthetic_cases(seed, count)
    output.mkdir(parents=True, exist_ok=False)
    (output / "cases.json").write_text(json.dumps(cases, indent=2) + "\n")
    actor = ActorContext(
        user_id="qualification",
        tenant_id="synthetic",
        project_id="rules",
        data_domains=("synthetic",),
    )
    records = []
    # Public disposable fixture key; the generated cases contain no private data.
    with open_protected_checkpointer(
        output / "checkpoints.sqlite", encryption_key=b"synthetic-rule-qualification-key!"[:32]
    ) as saver:
        for case in cases:
            expected_ids, mean = independent_expected(case["annex"], case["request"])
            adapter = AnnexWorkflowServices(case["annex"], documents=DOCUMENTS)
            flow = RuleWorkflowRuntime(
                services=adapter.services(),
                checkpointer=saver,
                source_hashes=adapter.source_hashes,
                workflow_version="synthetic-facet-qualification-v1",
            )
            try:
                thread, state = flow.run(RuleAnalysisRequest.model_validate(case["request"]), actor)
            except Exception as exc:
                records.append(
                    dict(
                        case_id=case["case_id"],
                        passed=False,
                        error_type=type(exc).__name__,
                        expected_ids=expected_ids,
                        observed_ids=None,
                        expected_mean_rational=str(mean) if mean is not None else None,
                        observed_mean=None,
                        source_hashes=adapter.source_hashes(),
                    )
                )
                continue
            delivery = state.get("delivery", {})
            actual_mean = delivery.get("average_fee")
            # Bound Decimal division error independently using exact rational differences.
            arithmetic_ok = (
                actual_mean is None
                if mean is None
                else actual_mean is not None
                and abs(Fraction(Decimal(actual_mean)) - mean) <= Fraction(1, 10**24)
            )
            records.append(
                dict(
                    case_id=case["case_id"],
                    thread_id=thread,
                    passed=state["status"] == "completed"
                    and delivery.get("items") == expected_ids
                    and arithmetic_ok,
                    expected_ids=expected_ids,
                    observed_ids=delivery.get("items"),
                    expected_mean_rational=str(mean) if mean is not None else None,
                    observed_mean=actual_mean,
                    source_hashes=adapter.source_hashes(),
                )
            )
    report = dict(
        suite="synthetic-facet-qualification-v1",
        seed=seed,
        n=len(records),
        passed=sum(row["passed"] for row in records),
        results=records,
        corpus_sha256=hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        model_calls=0,
        benchmark_score=False,
        production_accepted=False,
        limitations=[
            "select and average_fee only",
            "synthetic structured requests",
            "no independent human review or unseen natural-language evaluation",
        ],
    )
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
