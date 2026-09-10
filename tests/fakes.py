class FakeHttp:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], list[object]] = {}
        self.calls: list[tuple[str, str, object]] = []

    def queue_json(self, url: str, value: object, method: str = "GET") -> None:
        self.responses.setdefault((method, url), []).append(value)

    def queue_error(self, url: str, error: Exception, method: str = "GET") -> None:
        self.responses.setdefault((method, url), []).append(error)

    async def get_json(self, url: str, params: dict | None = None) -> object:
        self.calls.append(("GET", url, params))
        value = self.responses[("GET", url)].pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def post_json(self, url: str, payload: dict) -> object:
        self.calls.append(("POST", url, payload))
        value = self.responses[("POST", url)].pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeRpc:
    def __init__(self) -> None:
        self.responses: dict[str, list[object]] = {}
        self.calls: list[tuple[str, list]] = []

    def result(self, method: str, value: object) -> None:
        self.responses.setdefault(method, []).append(value)

    async def call(self, method: str, params: list) -> object:
        self.calls.append((method, params))
        value = self.responses[method].pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def calls_for(self, method: str) -> list[list]:
        return [
            params
            for called_method, params in self.calls
            if called_method == method
        ]


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, content: str) -> None:
        self.messages.append(content)


class FakePorCollector:
    def __init__(self) -> None:
        self.values: list[object] = []

    def queue_error(self, error: Exception) -> None:
        self.values.append(error)

    def queue_snapshot(self, value: object) -> None:
        self.values.append(value)

    async def collect(self, collected_at):
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeSupplyCollector:
    def __init__(self) -> None:
        self.values: list[object] = []

    def queue_error(self, error: Exception) -> None:
        self.values.append(error)

    def queue_global_supply(self, value: float) -> None:
        self.values.append(value)

    def queue_batch(self, batch: object) -> None:
        self.values.append(batch)

    async def collect(self, collected_at):
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        if not isinstance(value, (int, float)):
            return value
        from usd1_monitor.collectors.supply import SupplySnapshot
        from usd1_monitor.models import Observation

        observation = Observation(
            "supply.global",
            "defillama",
            "global",
            value,
            "USD1",
            collected_at,
            collected_at,
            quality="ESTIMATED_SOURCE",
        )
        return [SupplySnapshot("global", value, collected_at, observation)]
