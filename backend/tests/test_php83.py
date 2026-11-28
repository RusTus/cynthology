import os

os.environ["CYN_DEV"] = "1"

from cynthology import php83  # noqa: E402


def test_pin_only_php83():
    pin = php83.render_pin()
    blocks = [b for b in pin.split("\n\n") if "Package:" in b]
    assert "Package: *" in blocks[0] and "Pin-Priority: -1" in blocks[0]
    assert "Package: php8.3 php8.3-* php-common" in blocks[1] and "Pin-Priority: 500" in blocks[1]
    assert len(php83.FINGERPRINT) == 40
