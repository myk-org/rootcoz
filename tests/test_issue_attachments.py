"""Attachment foundation contract tests. No tracker or server writes."""

import base64
from uuid import uuid4

import pytest
from pydantic import ValidationError

from rootcoz import models, storage


def upload(name="evidence.log", data=b"log\x1b[0m\b\n"):
    return models.IssueAttachmentUpload(
        filename=name, data_b64=base64.b64encode(data).decode()
    )


def test_upload_schema():
    with pytest.raises(ValidationError):
        models.IssueAttachmentUpload(filename="x", data_b64="eA==", mime="text/plain")
    assert models.CreateIssueRequest(
        test_name="x", title="t", body="b", attachments=[upload()]
    ).attachments


def test_validation_and_safe_metadata():
    from rootcoz.issue_attachments import content_disposition, validate_attachments

    item = validate_attachments([upload("../evidence.log")])[0]
    assert item.display_name == "evidence.log"
    assert item.media_type == "text/plain"
    assert "filename*=" in content_disposition(item.display_name, str(uuid4()))


@pytest.mark.parametrize(
    "name,data,expected_reason",
    [
        ("x.svg", b"<svg/>", "unsupported file extension"),
        ("x.txt", b"%PDF-1.7", "container or executable content"),
        ("x.log", b"\x7f", "NUL or DEL in text"),
        ("x.txt", b"", "attachment is empty"),
        ("x.png", b"hello", "file content does not match extension"),
        ("x.txt", b"\x00", "NUL or DEL in text"),
        ("x.txt", b"\xff", "text attachment is not valid UTF-8"),
        ("x" * 121 + ".txt", b"x", "invalid display name; maximum 120 characters"),
        ("x\n.txt", b"x", "invalid display name; maximum 120 characters"),
        ("x.txt", b"\x1f\x8babc", "container or executable content"),
        ("x.txt", b"MZexecutable", "container or executable content"),
        (
            "x.jpg",
            b"\xff\xd8\xff" + b"x" * 20 + b"\xff\xd9",
            "text attachment is not valid UTF-8",
        ),
        (
            "x.gif",
            b"GIF89a" + b"x" * 20 + b";",
            "file content does not match extension",
        ),
    ],
)
def test_refuses_unsafe_content_with_specific_reason(name, data, expected_reason):
    from rootcoz.issue_attachments import IssueAttachmentRejected, validate_attachments

    with pytest.raises(IssueAttachmentRejected) as exc:
        validate_attachments([upload(name, data)])
    msg = str(exc.value)
    assert expected_reason in msg
    assert "0xff" not in msg
    assert "MZexecutable" not in msg


def test_limits_and_aggregate_refusal():
    from rootcoz.issue_attachments import IssueAttachmentRejected, validate_attachments

    with pytest.raises(IssueAttachmentRejected) as exc:
        validate_attachments(
            [
                upload("first.svg", b"<svg/>"),
                models.IssueAttachmentUpload(filename="second.txt", data_b64="!!!!"),
                upload("third.log", b"%PDF-1.7"),
            ]
        )
    msg = str(exc.value)
    assert "first.svg: unsupported file extension" in msg
    assert "second.txt: invalid base64 encoding" in msg
    assert "third.log: container or executable content" in msg
    with pytest.raises(IssueAttachmentRejected) as count_exc:
        validate_attachments([upload()] * 4)
    assert "At most 3 attachments are allowed" in str(count_exc.value)
    with pytest.raises(IssueAttachmentRejected) as non_ascii_b64_exc:
        validate_attachments(
            [models.IssueAttachmentUpload(filename="x.txt", data_b64="é===")]
        )
    assert "x.txt: invalid base64 encoding" in str(non_ascii_b64_exc.value)
    assert "ASCII" not in str(non_ascii_b64_exc.value)


def test_jira_attachments_section_has_no_stray_backslashes():
    from rootcoz.issue_attachments import attachments_section

    id_plain = str(uuid4())
    id_special = str(uuid4())
    section = attachments_section(
        [
            {"id": id_plain, "display_name": "my-log_file.txt"},
            {"id": id_special, "display_name": "a|b[1]{x}.txt"},
        ],
        "https://rootcoz.example.com",
        tracker="jira",
    )
    assert (
        f"* [my-log_file.txt|https://rootcoz.example.com/api/issue-attachments/{id_plain}]"
        in section
    )
    assert (
        f"* [a_b_1__x_.txt|https://rootcoz.example.com/api/issue-attachments/{id_special}]"
        in section
    )
    assert "\\" not in section


@pytest.mark.asyncio
async def test_reserved_store_open_and_exact_cleanup(initialized_db):
    from rootcoz.issue_attachments import (
        delete_stored,
        open_attachment,
        store,
        validate_attachments,
    )

    await storage.create_user("attachment-owner", role="reviewer")
    owner = await storage.get_user_by_username("attachment-owner")
    items = validate_attachments([upload()])
    rows = await storage.reserve_issue_attachments(
        items, owner_id=owner["id"], job_id="", tracker="github"
    )
    assert (await storage.get_issue_attachment(rows[0]["id"]))["state"] == "reserved"
    await store(rows, items)
    row = await storage.get_issue_attachment(rows[0]["id"])
    with open_attachment(row) as stream:
        assert stream.read() == items[0].data
    path = (
        storage.current_db_path().parent
        / "issue-attachments"
        / "users"
        / str(owner["id"])
        / row["id"]
    )
    path.unlink()
    path.symlink_to("/etc/passwd")
    with pytest.raises(OSError):
        open_attachment(row)
    await delete_stored(rows)
    assert await storage.get_issue_attachment(row["id"]) is None


@pytest.mark.asyncio
async def test_owner_validation_and_runless_cleanup_predicate(initialized_db):
    from rootcoz.issue_attachments import validate_attachments

    items = validate_attachments([upload()])
    with pytest.raises(ValueError):
        await storage.reserve_issue_attachments(
            items, owner_id=0, job_id="", tracker="github"
        )
    await storage.create_user("attachment-owner", role="reviewer")
    owner = await storage.get_user_by_username("attachment-owner")
    rows = await storage.reserve_issue_attachments(
        items, owner_id=owner["id"], job_id="", tracker="jira"
    )
    await storage.delete_issue_attachment_rows_for_user(owner["id"])
    assert await storage.get_issue_attachment(rows[0]["id"]) is None


@pytest.mark.parametrize("name", ["x\u0085.txt", "\ud800.txt", ".hidden.txt"])
def test_refuses_nonportable_names(name):
    from rootcoz.issue_attachments import IssueAttachmentRejected, validate_attachments

    with pytest.raises(IssueAttachmentRejected):
        validate_attachments([upload(name)])


@pytest.mark.asyncio
async def test_disabled_owner_refused(initialized_db):
    from rootcoz.issue_attachments import validate_attachments

    await storage.create_user("disabled-owner", role="reviewer")
    owner = await storage.get_user_by_username("disabled-owner")
    async with storage._connect_db() as db:
        await db.execute("UPDATE users SET disabled=1 WHERE id=?", (owner["id"],))
        await db.commit()
    with pytest.raises(ValueError):
        await storage.reserve_issue_attachments(
            validate_attachments([upload()]),
            owner_id=owner["id"],
            job_id="",
            tracker="github",
        )


@pytest.mark.asyncio
async def test_no_overwrite_on_existing_blob(initialized_db):
    from rootcoz.issue_attachments import store, validate_attachments

    await storage.create_user("owner", role="reviewer")
    owner = await storage.get_user_by_username("owner")
    items = validate_attachments([upload()])
    rows = await storage.reserve_issue_attachments(
        items, owner_id=owner["id"], job_id="", tracker="github"
    )
    path = (
        storage.current_db_path().parent
        / "issue-attachments"
        / "users"
        / str(owner["id"])
        / rows[0]["id"]
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b"preexisting evidence")
    with pytest.raises(FileExistsError):
        await store(rows, items)
    assert path.read_bytes() == b"preexisting evidence"


@pytest.mark.asyncio
async def test_quota_serialization_and_owner_isolation(initialized_db):
    import asyncio

    from rootcoz.issue_attachments import validate_attachments

    await storage.create_user("quota-owner", role="reviewer")
    owner = await storage.get_user_by_username("quota-owner")
    items = validate_attachments([upload(data=b"xx")])
    row = (
        await storage.reserve_issue_attachments(
            items, owner_id=owner["id"], job_id="", tracker="github"
        )
    )[0]
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE issue_attachments SET size_bytes=? WHERE id=?",
            (500 * 1024 * 1024 - 2, row["id"]),
        )
        await db.commit()

    async def reserve():
        return await storage.reserve_issue_attachments(
            items, owner_id=owner["id"], job_id="", tracker="github"
        )

    results = await asyncio.gather(reserve(), reserve(), return_exceptions=True)
    assert sum(isinstance(r, ValueError) for r in results) == 1
    await storage.create_user("other-owner", role="reviewer")
    other = await storage.get_user_by_username("other-owner")
    assert await storage.reserve_issue_attachments(
        items, owner_id=other["id"], job_id="", tracker="github"
    )


@pytest.mark.asyncio
async def test_run_rows_survive_user_metadata_cleanup(initialized_db):
    from rootcoz.issue_attachments import validate_attachments

    await storage.create_user("owner", role="reviewer")
    owner = await storage.get_user_by_username("owner")
    job = str(uuid4())
    async with storage._connect_db() as db:
        await db.execute("INSERT INTO results(job_id) VALUES (?)", (job,))
        await db.commit()
    rows = await storage.reserve_issue_attachments(
        validate_attachments([upload()]),
        owner_id=owner["id"],
        job_id=job,
        tracker="jira",
    )
    await storage.delete_issue_attachment_rows_for_user(owner["id"])
    assert await storage.get_issue_attachment(rows[0]["id"])
    await storage.init_db()
    assert await storage.get_issue_attachment(rows[0]["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["issue-attachments", "users", "owner"])
async def test_parent_symlinks_refused(initialized_db, tmp_path, component):
    from rootcoz.issue_attachments import open_attachment

    root = storage.current_db_path().parent / "issue-attachments"
    parts = {
        "issue-attachments": root,
        "users": root / "users",
        "owner": root / "users" / "1",
    }
    path = parts[component]
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target"
    target.mkdir()
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        open_attachment(
            {"id": str(uuid4()), "owner_id": 1, "job_id": "", "size_bytes": 1}
        )


@pytest.mark.asyncio
async def test_failed_cleanup_is_retryable(initialized_db, monkeypatch):
    import errno

    from rootcoz import issue_attachments as attachments

    await storage.create_user("owner", role="reviewer")
    owner = await storage.get_user_by_username("owner")
    items = attachments.validate_attachments([upload()])
    rows = await storage.reserve_issue_attachments(
        items, owner_id=owner["id"], job_id="", tracker="github"
    )
    await attachments.store(rows, items)
    original = attachments._unlink

    def refuse(row):
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(attachments, "_unlink", refuse)
    await attachments.delete_stored(rows)
    assert (await storage.get_issue_attachment(rows[0]["id"]))[
        "state"
    ] == "cleanup_pending"
    monkeypatch.setattr(attachments, "_unlink", original)
    await attachments.delete_stored(rows)
    assert await storage.get_issue_attachment(rows[0]["id"]) is None


@pytest.mark.asyncio
async def test_open_fd_survives_path_replacement(initialized_db):
    import stat

    from rootcoz.issue_attachments import open_attachment, store, validate_attachments

    await storage.create_user("owner", role="reviewer")
    owner = await storage.get_user_by_username("owner")
    items = validate_attachments([upload()])
    rows = await storage.reserve_issue_attachments(
        items, owner_id=owner["id"], job_id="", tracker="github"
    )
    await store(rows, items)
    path = (
        storage.current_db_path().parent
        / "issue-attachments"
        / "users"
        / str(owner["id"])
        / rows[0]["id"]
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o660
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o770
    with open_attachment(rows[0]) as stream:
        path.unlink()
        path.symlink_to("/etc/passwd")
        assert stream.read() == items[0].data
    assert stream.closed


@pytest.mark.asyncio
async def test_upgrade_pre_attachment_schema(initialized_db):
    job = str(uuid4())
    async with storage._connect_db() as db:
        await db.execute("DROP TABLE issue_attachments")
        await db.execute(
            "INSERT INTO results(job_id, result_json) VALUES (?, ?)",
            (job, '{"sentinel": true}'),
        )
        await db.commit()
    await storage.init_db()
    async with storage._connect_db() as db:
        cursor = await db.execute(
            "SELECT result_json FROM results WHERE job_id=?", (job,)
        )
        assert (await cursor.fetchone())[0] == '{"sentinel": true}'
        cursor = await db.execute("SELECT COUNT(*) FROM issue_attachments")
        assert (await cursor.fetchone())[0] == 0


def test_size_boundaries_and_png_family():
    from rootcoz.issue_attachments import IssueAttachmentRejected, validate_attachments

    item = upload(data=b"x" * (5 * 1024 * 1024))
    assert len(validate_attachments([item, item])) == 2
    with pytest.raises(IssueAttachmentRejected):
        validate_attachments([item, item, upload()])
    with pytest.raises(IssueAttachmentRejected):
        validate_attachments([upload(data=b"x" * (5 * 1024 * 1024 + 1))])
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg=="
    )
    assert validate_attachments([upload("x.png", png)])[0].media_type == "image/png"
    with pytest.raises(IssueAttachmentRejected):
        validate_attachments([upload("x.txt", png)])
