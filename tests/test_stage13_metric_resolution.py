from fastapi.testclient import TestClient
from test_metric_analysis import build_runtime

from generaldataagent.api.app import create_app
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.contracts import ActorContext
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.orchestration.metric_resolution import CertifiedMetricResolver
from generaldataagent.security.identity import SessionBroker


def session(
    broker: SessionBroker,
    *,
    data_domains: tuple[str, ...] = ("commerce",),
    permissions: frozenset[str] = frozenset({"analysis:run"}),
) -> dict[str, str]:
    owner = ActorContext(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=data_domains,
    )
    token = broker.issue(
        user_id=owner.user_id,
        tenant_id=owner.tenant_id,
        project_id=owner.project_id,
        data_domains=frozenset(data_domains),
        permissions=permissions,
        ttl_seconds=900,
    )
    return {
        "authorization": f"Bearer {token}",
        "x-user-id": owner.user_id,
        "x-tenant-id": owner.tenant_id,
        "x-project-id": owner.project_id,
        "x-data-domain": owner.data_domains[0],
    }


def client(runtime: MetricAnalysisRuntime, broker: SessionBroker) -> TestClient:
    return TestClient(
        create_app(
            metric_runtime=runtime,
            session_broker=broker,
            metric_resolution=CertifiedMetricResolver(runtime.catalog),
        )
    )


def assert_no_execution_side_effects(tmp_path) -> None:
    assert not (tmp_path / "jobs.db").exists()
    assert not (tmp_path / "checkpoint.db").exists()
    artifacts = tmp_path / "artifacts"
    assert not artifacts.exists() or not any(artifacts.iterdir())
    assert not (tmp_path / "audit" / "events.jsonl").exists()


def test_resolves_one_authorized_metric_without_creating_any_execution(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    broker = SessionBroker()
    api = client(runtime, broker)

    response = api.post(
        "/v1/analysis/metrics/resolve",
        headers=session(broker),
        json={"question": "订单量"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "schema_version": "1.0",
        "status": "resolved",
        "candidates": [
            {
                "schema_version": "1.0",
                "metric_name": "order_count",
                "metric_version": "v1",
                "description": "Number of accepted orders",
                "unit": "count",
                "matched_alias": "订单量",
            }
        ],
        "clarification_question": None,
        "scope": "certified-metric-catalog-read-only",
        "execution": "not-started",
        "task_created": False,
        "query_compiled": False,
        "artifacts_created": False,
        "budget_charged": False,
        "model_invoked": False,
    }
    assert "question" not in body
    capabilities = api.get("/v1/capabilities", headers=session(broker))
    assert capabilities.json()["certified_metric_resolution"] is True
    assert_no_execution_side_effects(tmp_path)


def test_resolution_is_narrowed_to_the_caller_declared_data_domains(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    broker = SessionBroker()
    api = client(runtime, broker)
    headers = session(broker, data_domains=("commerce", "finance"))

    narrowed = dict(headers, **{"x-data-domain": "finance"})
    denied = api.post(
        "/v1/analysis/metrics/resolve",
        headers=narrowed,
        json={"question": "订单量"},
    )
    assert denied.status_code == 200
    assert denied.json()["status"] == "not_found"
    assert denied.json()["candidates"] == []

    allowed = api.post(
        "/v1/analysis/metrics/resolve",
        headers=dict(headers, **{"x-data-domain": "commerce"}),
        json={"question": "订单量"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "resolved"
    assert_no_execution_side_effects(tmp_path)


def test_ambiguous_alias_returns_explicit_choices_and_no_task(tmp_path) -> None:
    original = build_runtime(tmp_path)
    base = original.catalog.get("order_count", "v1")
    catalog = InMemoryMetricCatalog(
        [
            base,
            MetricDefinition(
                name="order_total",
                version="v1",
                description="Total accepted orders",
                aliases=("订单量",),
                data_domain="commerce",
                relation="orders",
                expression="COUNT(*)",
                time_column="created_at",
                owner="commerce-data",
            ),
        ]
    )
    runtime = MetricAnalysisRuntime(
        catalog=catalog,
        executor=original.executor,
        artifacts=original.artifacts,
        audit=original.audit,
    )
    broker = SessionBroker()
    api = client(runtime, broker)

    response = api.post(
        "/v1/analysis/metrics/resolve",
        headers=session(broker),
        json={"question": "订单量", "metric_version": "v1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ambiguous"
    assert [item["metric_name"] for item in body["candidates"]] == [
        "order_count",
        "order_total",
    ]
    assert body["clarification_question"] == (
        "Choose one certified metric: order_count@v1, order_total@v1."
    )
    assert body["task_created"] is False
    assert_no_execution_side_effects(tmp_path)


def test_not_found_and_version_filter_do_not_disclose_catalog_entries(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    broker = SessionBroker()
    api = client(runtime, broker)
    headers = session(broker)

    missing = api.post(
        "/v1/analysis/metrics/resolve",
        headers=headers,
        json={"question": "退款金额"},
    )
    assert missing.status_code == 200
    assert missing.json()["status"] == "not_found"
    assert missing.json()["candidates"] == []

    wrong_version = api.post(
        "/v1/analysis/metrics/resolve",
        headers=headers,
        json={"question": "订单量", "metric_version": "v2"},
    )
    assert wrong_version.status_code == 200
    assert wrong_version.json()["status"] == "not_found"
    assert wrong_version.json()["candidates"] == []
    assert_no_execution_side_effects(tmp_path)


def test_resolution_request_rejects_query_options_and_unknown_fields(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    broker = SessionBroker()
    api = client(runtime, broker)
    headers = session(broker)

    for body in (
        {"question": "订单量", "dimensions": ["channel"]},
        {"question": "订单量", "start_time": "2026-10-01T00:00:00+00:00"},
        {"question": "订单量", "unexpected": True},
        {"question": ""},
    ):
        response = api.post("/v1/analysis/metrics/resolve", headers=headers, json=body)
        assert response.status_code == 422
    assert_no_execution_side_effects(tmp_path)


def test_resolution_requires_session_scope_and_analysis_permission(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    broker = SessionBroker()
    api = client(runtime, broker)

    assert (
        api.post(
            "/v1/analysis/metrics/resolve",
            headers={"x-data-domain": "commerce"},
            json={"question": "订单量"},
        ).status_code
        == 401
    )
    assert (
        api.post(
            "/v1/analysis/metrics/resolve",
            headers=session(broker, permissions=frozenset()),
            json={"question": "订单量"},
        ).status_code
        == 403
    )
    finance = session(broker, data_domains=("finance",))
    assert (
        api.post(
            "/v1/analysis/metrics/resolve",
            headers=dict(finance, **{"x-data-domain": "commerce"}),
            json={"question": "订单量"},
        ).status_code
        == 403
    )
    assert_no_execution_side_effects(tmp_path)


def test_resolution_is_disabled_unless_explicitly_configured(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    broker = SessionBroker()
    api = TestClient(create_app(metric_runtime=runtime, session_broker=broker))

    response = api.post(
        "/v1/analysis/metrics/resolve",
        headers=session(broker),
        json={"question": "订单量"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "metric_resolution_not_configured"
    capabilities = api.get("/v1/capabilities", headers=session(broker))
    assert capabilities.json()["certified_metric_resolution"] is False
    assert_no_execution_side_effects(tmp_path)


def test_resolution_configuration_fails_closed(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    resolver = CertifiedMetricResolver(runtime.catalog)
    broker = SessionBroker()

    try:
        create_app(
            metric_runtime=runtime,
            session_broker=broker,
            metric_resolution=resolver,
            production_mode=True,
        )
    except ValueError as exc:
        assert str(exc) == "local_metric_resolution_not_qualified_for_production"
    else:
        raise AssertionError("production must reject a local resolver")

    try:
        create_app(metric_runtime=runtime, metric_resolution=resolver)
    except ValueError as exc:
        assert str(exc) == "metric_resolution_requires_authenticated_sessions"
    else:
        raise AssertionError("a resolver requires authenticated sessions")

    other = InMemoryMetricCatalog([runtime.catalog.get("order_count", "v1")])
    try:
        create_app(
            metric_runtime=runtime,
            session_broker=broker,
            metric_resolution=CertifiedMetricResolver(other),
        )
    except ValueError as exc:
        assert str(exc) == "metric_resolution_must_share_runtime_catalog"
    else:
        raise AssertionError("the resolver must share the runtime catalog")

    try:
        CertifiedMetricResolver({})
    except TypeError as exc:
        assert str(exc) == "certified metric resolution requires a certified catalog"
    else:
        raise AssertionError("only a certified catalog is accepted")
