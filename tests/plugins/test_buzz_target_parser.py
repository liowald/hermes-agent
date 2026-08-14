from plugins.platforms.buzz.adapter import _parse_buzz_target, _validate_buzz_target


def test_buzz_explicit_uuid_target_is_accepted_without_directory_lookup():
    target = "7F2E74FE-405C-45B6-BDCC-F78BC518B65A"
    assert _parse_buzz_target(target) == (target.lower(), None)
    assert _validate_buzz_target(target) is True


def test_buzz_non_uuid_target_is_rejected():
    assert _parse_buzz_target("reader") is None
    assert _validate_buzz_target("reader") == "expected a channel UUID"
