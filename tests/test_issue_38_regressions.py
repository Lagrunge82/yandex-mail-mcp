from email.message import EmailMessage
import asyncio

from yandex_mail_mcp import server
from yandex_mail_mcp.mail import parse_message, _text_sections


def test_root_mime_entity_and_bearer_scheme_regressions():
    msg = EmailMessage()
    msg.set_content("gateway body")
    msg.replace_header("Content-Type", 'text/plain; name="body.txt"')
    parsed = parse_message(msg.as_bytes(), 1)
    assert parsed["preview"] == "gateway body"
    assert parsed["has_attachments"] is False

    structure = (b"TEXT", b"PLAIN", (b"NAME", b"body.txt"), None, None, b"7BIT", 12, 1, None, (b"ATTACHMENT", (b"FILENAME", b"body.txt")))
    sections, has_attachments = _text_sections(structure)
    assert sections and has_attachments is False

    accepted = []

    async def app(scope, receive, send):
        accepted.append(True)

    async def send(_message):
        pass

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(server.BearerAuth(app, "secret")(
        {"type": "http", "headers": [(b"authorization", b"bearer secret")]}, receive, send
    ))
    assert accepted
