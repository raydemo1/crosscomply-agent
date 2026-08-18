"""Tests for immutable original-material object storage."""

from io import BytesIO

from law_agent.review.object_store import MaterialObjectStore


class FakeMinio:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def put_object(self, bucket: str, key: str, stream: BytesIO, size: int, **_: object) -> None:
        self.objects[(bucket, key)] = stream.read(size)

    def get_object(self, bucket: str, key: str) -> BytesIO:
        return BytesIO(self.objects[(bucket, key)])


def test_original_key_and_hash_are_content_addressed_and_case_scoped() -> None:
    client = FakeMinio()
    store = MaterialObjectStore(client=client, bucket="crosscomply-materials")

    stored = store.put_original(
        case_id="case_001",
        logical_name="vendor_dpa",
        filename="DPA 合同.pdf",
        content_type="application/pdf",
        content=b"immutable-original",
    )

    assert stored.sha256 == "84623405f03b6687bf0066b394e76a7e0020a81706a7bbbee24fb964cc67ab5d"
    assert stored.object_key == (
        "cases/case_001/materials/vendor_dpa/"
        "84623405f03b6687bf0066b394e76a7e0020a81706a7bbbee24fb964cc67ab5d.pdf"
    )
    assert stored.byte_size == 18
    assert client.objects[("crosscomply-materials", stored.object_key)] == b"immutable-original"
    assert store.get_original(stored.object_key) == b"immutable-original"


def test_put_original_is_idempotent_for_same_version_and_content() -> None:
    client = FakeMinio()
    store = MaterialObjectStore(client=client, bucket="crosscomply-materials")
    first = store.put_original(
        case_id="case_001",
        logical_name="data_inventory",
        filename="data.csv",
        content_type="text/csv",
        content=b"a,b\n1,2\n",
    )
    second = store.put_original(
        case_id="case_001",
        logical_name="data_inventory",
        filename="data.csv",
        content_type="text/csv",
        content=b"a,b\n1,2\n",
    )

    assert second == first
    assert len(client.objects) == 1


def test_chinese_logical_name_uses_safe_deterministic_path_segment() -> None:
    client = FakeMinio()
    store = MaterialObjectStore(client=client, bucket="crosscomply-materials")

    stored = store.put_original(
        case_id="case_001",
        logical_name="采购合同",
        filename="contract.pdf",
        content_type="application/pdf",
        content=b"contract",
    )

    assert stored.object_key.startswith("cases/case_001/materials/")
    assert "采购合同" not in stored.object_key
