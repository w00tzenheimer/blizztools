from blizztools.cdn import parse_build_config
from blizztools.models import Md5Hash
from blizztools.parsers import (
    parse_cdn_table,
    parse_named_attribute,
    parse_named_attribute_pair,
    parse_version_table,
)


def test_parse_named_attribute():
    lines = [b"root = 12345"]
    val = parse_named_attribute(b"root", lines, value_type=int)
    assert val == 12345


def test_parse_named_attribute_pair():
    lines = [b"install = 123 456"]
    val1, val2 = parse_named_attribute_pair(b"install", lines, value_type=int)
    assert val1 == 123
    assert val2 == 456


def test_parse_version_table():
    data = """
# Version Table
!VERS!1
Region!BuildConfig!CDNConfig!KeyRing!BuildId!VersionName!ProductConfig
eu|b7b342cf87c3828f09fc81828b0d06c0|a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6||12345|1.0.0.12345|a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6
"""
    versions = parse_version_table(data)
    assert len(versions) == 1
    assert versions[0].region == "eu"
    assert versions[0].build_id == "12345"


def test_parse_cdn_table():
    data = """
# CDN Table
!CDNS!1
Name!Path!Hosts!Servers!ConfigPath
eu|tpr/wow|blizzard.com|server1.blizzard.com|config
"""
    cdns = parse_cdn_table(data)
    assert len(cdns) == 1
    assert cdns[0].name == "eu"
    assert cdns[0].path == "tpr/wow"


def test_parse_build_config():
    data = b"""# Build Configuration
root = 74260639df2c36f256dec1dc99007dee
install = cb771e4587a2e7d3df2aa0a0802a1fc9 5707c55346b2bdffdc12587673ca6e78
install-size = 17491 16957
download = 742820d6e2a8e08c657b2f6402f5beb3 0ee936e6e1c5eda32dad6e133eb24b02
download-size = 9391314 8189832
size = 04b685919f85d762322f635a207d85d2 1a98c149a20d884fe4a6d6ec507b0dcd
size-size = 6043993 5280643
encoding = 81d6b3444dbb7113f69c7625361dbb91 9ea78760c2cfe3c9c3ccd42bf2057f95
encoding-size = 23840656 23805555
"""
    config = parse_build_config(data)
    assert isinstance(config.root, Md5Hash)
    assert str(config.root) == "74260639df2c36f256dec1dc99007dee"
    assert config.install_size == (17491, 16957)
