import io
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image
from waitress import create_server

import drive.thumbnails
from conftest import OWNER, SignedInClient, make_app

SLOW_RENDER_SECONDS = 1.0
SERVER_THREADS = 8
BURST_IMAGES = 12
HEALTH_LIMIT_SECONDS = 0.6
QUICK_ANSWER_SECONDS = 0.6
EVENTUAL_SECONDS = 30
RETRY_PAUSE_SECONDS = 0.2
NOT_READY = 503


@pytest.fixture
def slow_renders(monkeypatch):
    rendering_now = []
    most_at_once = []
    guard = threading.Lock()
    real_render = drive.thumbnails.render_thumbnail

    def slow_render(source, target, mime):
        with guard:
            rendering_now.append(target)
            most_at_once.append(len(rendering_now))
        time.sleep(SLOW_RENDER_SECONDS)
        try:
            real_render(source, target, mime)
        finally:
            with guard:
                rendering_now.remove(target)

    monkeypatch.setattr(drive.thumbnails, "render_thumbnail", slow_render)
    return most_at_once


def png_bytes(shade: int) -> bytes:
    picture = io.BytesIO()
    Image.new("RGB", (640, 480), (shade, 90, 160)).save(picture, "PNG")
    return picture.getvalue()


@pytest.fixture
def images(services):
    owner = SignedInClient(make_app(services).test_client())
    return [owner.upload(f"photo-{number}.png", png_bytes(number * 10)) for number in range(BURST_IMAGES)]


@pytest.fixture
def served(services):
    server = create_server(make_app(services), host="127.0.0.1", port=0, threads=SERVER_THREADS)
    runner = threading.Thread(target=server.run, daemon=True)
    runner.start()
    try:
        yield f"http://127.0.0.1:{server.effective_port}"
    finally:
        server.close()
        server.task_dispatcher.shutdown()


def fetch(url: str) -> tuple[int, float, dict]:
    began = time.monotonic()
    request = urllib.request.Request(url, headers={"X-Forwarded-User": OWNER})
    try:
        with urllib.request.urlopen(request, timeout=EVENTUAL_SECONDS) as answer:
            answer.read()
            return answer.status, time.monotonic() - began, dict(answer.headers)
    except urllib.error.HTTPError as error:
        error.read()
        return error.code, time.monotonic() - began, dict(error.headers)


def test_health_answers_fast_while_a_grid_of_new_images_is_requested(served, images, slow_renders):
    thumb_urls = [f"{served}/api/files/{image['id']}/thumb" for image in images]
    with ThreadPoolExecutor(max_workers=len(thumb_urls)) as pool:
        burst = [pool.submit(fetch, url) for url in thumb_urls]
        time.sleep(SLOW_RENDER_SECONDS / 4)
        status, took, _ = fetch(f"{served}/api/health")
        results = [future.result() for future in burst]
    assert status == 200
    assert took < HEALTH_LIMIT_SECONDS
    assert all(code in (200, NOT_READY) for code, _, _ in results)


def test_renders_are_capped_and_the_rest_hear_not_ready_quickly(services, images, slow_renders):
    app = make_app(services)
    thumb_paths = [f"/api/files/{image['id']}/thumb" for image in images]

    def ask(path):
        began = time.monotonic()
        answer = SignedInClient(app.test_client()).get(path)
        return answer.status_code, time.monotonic() - began, answer.headers.get("Retry-After")

    with ThreadPoolExecutor(max_workers=len(thumb_paths)) as pool:
        results = list(pool.map(ask, thumb_paths))
    not_ready = [result for result in results if result[0] == NOT_READY]
    assert max(slow_renders) == drive.thumbnails.RENDERS_AT_ONCE
    assert len(not_ready) == len(thumb_paths) - drive.thumbnails.RENDERS_AT_ONCE
    assert all(took < QUICK_ANSWER_SECONDS and retry_after for _, took, retry_after in not_ready)


def test_every_thumbnail_arrives_when_the_page_keeps_asking(served, images, slow_renders):
    waiting = {f"{served}/api/files/{image['id']}/thumb" for image in images}
    deadline = time.monotonic() + EVENTUAL_SECONDS
    while waiting and time.monotonic() < deadline:
        with ThreadPoolExecutor(max_workers=len(waiting)) as pool:
            answers = dict(zip(waiting, pool.map(fetch, waiting)))
        waiting = {url for url, (status, _, _) in answers.items() if status != 200}
        assert all(status in (200, NOT_READY) for status, _, _ in answers.values())
        time.sleep(RETRY_PAUSE_SECONDS)
    assert waiting == set()


def test_the_same_new_image_asked_for_twice_is_rendered_once(services, images, slow_renders):
    app = make_app(services)
    path = f"/api/files/{images[0]['id']}/thumb"
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(lambda _: SignedInClient(app.test_client()).get(path).status_code, range(2)))
    assert codes == [200, NOT_READY]
    assert len(slow_renders) == 1


def test_a_picture_that_cannot_be_read_is_only_tried_once(services, owner, monkeypatch):
    calls = []
    real_render = drive.thumbnails.render_thumbnail
    monkeypatch.setattr(drive.thumbnails, "render_thumbnail", lambda *args: calls.append(args) or real_render(*args))
    broken = png_bytes(10)[:400]
    node = owner.upload("broken.png", broken)
    assert owner.get(f"/api/files/{node['id']}/thumb").status_code == 404
    assert owner.get(f"/api/files/{node['id']}/thumb").status_code == 404
    assert len(calls) == 1
