

def test_version_order_with_prereleases():
    from cynthology.update import vkey
    order = ["0.52.0-alpha", "0.53.0", "1.0.0-beta.1", "1.0.0-beta.2", "1.0.0-beta.10", "1.0.0-rc.1", "1.0.0", "1.0.1"]
    assert sorted(order, key=vkey) == order
