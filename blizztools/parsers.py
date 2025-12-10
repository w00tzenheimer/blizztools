from typing import List, Tuple, Type, TypeVar

from .models import Md5Hash

T = TypeVar("T")


class ParserError(Exception):
    pass


def _parse_key_value(name: bytes, lines: List[bytes]) -> Tuple[bytes, bytes]:
    try:
        line = lines.pop(0)
        key, value_bytes = line.split(b" = ", 1)
    except IndexError as e:
        raise ParserError(f"Expected attribute '{name.decode()}', got no lines") from e
    except ValueError as e:
        line_hex = line.hex()
        raise ParserError(
            f"Expected attribute '{name.decode()}', got '{line_hex}' but expected ' = '"
        ) from e

    if key.strip() != name:
        raise ParserError(
            f"Expected attribute '{name.decode()}', got '{key.decode()}' (and value: '{value_bytes.decode()}')"
        )
    return key, value_bytes


def parse_named_attribute(
    name: bytes, lines: List[bytes], value_type: Type[T] = bytes
) -> T:
    _, value_bytes = _parse_key_value(name, lines)
    value_str = value_bytes.strip().decode("utf-8")

    if value_type is Md5Hash:
        return Md5Hash(value_str)
    if value_type is int:
        return int(value_str)
    if value_type is bytes:
        return value_bytes.strip()
    return value_type(value_str)


def parse_named_attribute_pair(
    name: bytes, lines: List[bytes], value_type: Type[T] = bytes
) -> Tuple[T, T]:
    _, value_bytes = _parse_key_value(name, lines)
    value_str = value_bytes.strip().decode("utf-8")
    val1_str, val2_str = value_str.split(" ", 1)

    if value_type is Md5Hash:
        return Md5Hash(val1_str), Md5Hash(val2_str)
    if value_type is int:
        return int(val1_str), int(val2_str)
    if value_type is bytes:
        return val1_str.encode("utf-8"), val2_str.encode("utf-8")

    return value_type(val1_str), value_type(val2_str)


# from tact.rs


class VersionDefinition:
    def __init__(
        self,
        region,
        build_config,
        cdn_config,
        key_ring,
        build_id,
        version_name,
        product_config,
    ):
        self.region = region
        self.build_config = build_config
        self.cdn_config = cdn_config
        self.key_ring = key_ring
        self.build_id = build_id
        self.version_name = version_name
        self.product_config = product_config

    def __repr__(self):
        return f"VersionDefinition(region={self.region}, build_config={self.build_config}, cdn_config={self.cdn_config}, ...)"


class CdnDefinition:
    def __init__(self, name, path, hosts, servers, config_path):
        self.name = name
        self.path = path
        self.hosts = hosts
        self.servers = servers
        self.config_path = config_path

    def __repr__(self):
        return f"CdnDefinition(name={self.name}, path={self.path}, hosts={self.hosts}, servers={self.servers}, ...)"


def parse_version_table(data: str) -> List[VersionDefinition]:
    lines = data.strip().split("\n")
    header_index = -1
    for i, line in enumerate(lines):
        if line.startswith("Region!"):
            header_index = i
            break
    if header_index != -1:
        lines = lines[header_index + 1 :]

    lines = [line for line in lines if not line.startswith("#") and "|" in line]
    return [parse_version_table_entry(line) for line in lines]


def parse_cdn_table(data: str) -> List[CdnDefinition]:
    lines = data.strip().split("\n")
    header_index = -1
    for i, line in enumerate(lines):
        if line.startswith("Name!"):
            header_index = i
            break
    if header_index != -1:
        lines = lines[header_index + 1 :]

    lines = [line for line in lines if not line.startswith("#") and "|" in line]
    return [parse_cdn_table_entry(line) for line in lines]


def parse_cdn_table_entry(line: str) -> CdnDefinition:
    parts = line.split("|")
    name = parts[0]
    path = parts[1]
    hosts = parts[2].split(" ")
    servers = parts[3].split(" ")
    config_path = parts[4]
    return CdnDefinition(name, path, hosts, servers, config_path)


def parse_version_table_entry(line: str) -> VersionDefinition:
    parts = line.split("|")
    region = parts[0]
    build_config = Md5Hash(parts[1])
    cdn_config = Md5Hash(parts[2])
    key_ring_str = parts[3]
    key_ring = (
        Md5Hash(key_ring_str) if key_ring_str and len(key_ring_str) == 32 else None
    )
    build_id = parts[4]
    version_name = parts[5]
    product_config = Md5Hash(parts[6])
    return VersionDefinition(
        region,
        build_config,
        cdn_config,
        key_ring,
        build_id,
        version_name,
        product_config,
    )
