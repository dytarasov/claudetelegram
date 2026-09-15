"""Сервис самодопила на заглушках вместо git, systemd и БД.

Ради этого и затевались порты: логика «когда можно катить, что писать в файл
сторожу, как склеить историю с git log» проверяется без единого коммита,
перезапуска и подключения к Postgres.
"""
from __future__ import annotations

import json

import pytest

from app.domain.dto import DeployCommand, RollbackCommand
from app.domain.enums import DeployPhase
from app.domain.models import Deployment
from app.services.selfupdate import SelfUpdateService
from app.settings import Settings


class FakeGit:
    """Линейная история из трёх коммитов и метка stable на среднем."""

    def __init__(self):
        self.commits = {"c" * 40: "третий", "b" * 40: "второй", "a" * 40: "первый"}
        self.order = list(self.commits)
        self.tags = {"stable": "b" * 40}
        self.dirty_files = [(" M", "src/app/main.py")]
        self.committed: list[str] = []
        self.reset_to: str | None = None

    def head(self): return self.order[0]
    def short(self, ref): return (ref or "")[:7]
    def subject(self, ref): return self.commits.get(ref, "?")
    def resolve(self, ref):
        if ref in self.tags:
            return self.tags[ref]
        if ref.isdigit():
            return self.order[int(ref)]
        return ref if ref in self.commits else self.order[0]
    def dirty(self): return list(self.dirty_files)
    def commit(self, message, paths):
        sha = "d" * 40
        self.commits[sha] = message
        self.order.insert(0, sha)
        self.committed.append(message)
        return sha
    def tag(self, name, ref): self.tags[name] = ref
    def tag_sha(self, name): return self.tags.get(name)
    def log(self, limit):
        return [{"sha": s, "short": s[:7], "subject": self.commits[s], "when": "час назад"}
                for s in self.order[:limit]]
    def reset_hard(self, ref): self.reset_to = ref


class FakeSystemd:
    def __init__(self):
        self.spawned: list[list[str]] = []

    def restart(self, unit): pass
    def is_active(self, unit): return True
    def spawn_detached(self, name, argv):
        self.spawned.append(list(argv))
        return f"{name}-1"


class FakeDeployments:
    def __init__(self):
        self.items: list[Deployment] = []
        self.flight: Deployment | None = None

    async def add(self, deployment):
        deployment.id = len(self.items) + 1
        self.items.append(deployment)
        return deployment
    async def update(self, deployment):
        self.items = [deployment if d.id == deployment.id else d for d in self.items]
    async def get(self, deployment_id):
        return next((d for d in self.items if d.id == deployment_id), None)
    async def by_sha(self, sha):
        return next((d for d in self.items if d.new_sha == sha), None)
    async def in_flight(self): return self.flight
    async def recent(self, limit=20): return self.items[-limit:]


@pytest.fixture
def service(tmp_path):
    # root_dir во временный каталог — иначе тест напишет в боевой run/update.json
    # и собьёт метку реальной выкатки. Один раз уже наступили.
    settings = Settings(_env_file=None, telegram_bot_token="t", allowed_user_ids="1",
                        database_dsn="d", root_dir=tmp_path)
    git, systemd, repo = FakeGit(), FakeSystemd(), FakeDeployments()
    svc = SelfUpdateService(settings, git, systemd, repo)
    return svc, git, systemd, repo, settings


async def test_deploy_refuses_while_another_is_in_flight(service):
    svc, _git, systemd, repo, _s = service
    repo.flight = Deployment(note="едет", prev_sha="a", new_sha="b",
                             phase=DeployPhase.SOAKING, id=7)
    result = await svc.deploy(DeployCommand(note="ещё одна"))
    assert not result.ok
    assert "#7" in result.message
    assert systemd.spawned == []


async def test_deploy_stops_when_preflight_fails(service, monkeypatch):
    from app.domain.dto import PreflightReport
    svc, git, systemd, _repo, _s = service
    monkeypatch.setattr(svc, "preflight", lambda: _report(["импорт: нет модуля"]))
    result = await svc.deploy(DeployCommand(note="сломанное"))
    assert not result.ok
    assert git.committed == []          # ничего не закоммичено
    assert systemd.spawned == []        # сторож не запускался
    assert result.preflight.problems == ["импорт: нет модуля"]
    assert isinstance(result.preflight, PreflightReport)


async def _report(problems):
    from app.domain.dto import PreflightReport
    return PreflightReport(problems=problems, checks={"импорт": not problems})


async def test_deploy_commits_writes_marker_and_spawns_guard(service, monkeypatch):
    svc, git, systemd, repo, settings = service
    monkeypatch.setattr(svc, "preflight", lambda: _report([]))
    result = await svc.deploy(DeployCommand(note="правка", chat_id=42, soak_s=30))

    assert result.ok and result.new_sha == "d" * 40
    assert git.committed == ["selfupdate: правка"]
    assert repo.items[0].phase is DeployPhase.PENDING

    marker = json.loads(settings.update_file.read_text())
    assert marker["chat_id"] == 42
    assert marker["deployment_id"] == repo.items[0].id
    assert marker["announced"] is False

    argv = systemd.spawned[0]
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "deploy"
    assert argv[argv.index("--soak") + 1] == "30"


async def test_deploy_says_nothing_to_do_on_clean_tree(service, monkeypatch):
    svc, git, _systemd, _repo, _s = service
    git.dirty_files = []
    monkeypatch.setattr(svc, "preflight", lambda: _report([]))
    result = await svc.deploy(DeployCommand(note="пусто"))
    assert not result.ok and "чистое" in result.message


async def test_rollback_targets_stable_by_default(service):
    svc, git, systemd, repo, settings = service
    result = await svc.rollback(RollbackCommand(chat_id=1))
    assert result.ok and result.new_sha == git.tags["stable"]
    assert repo.items[0].phase is DeployPhase.ROLLING_BACK
    argv = systemd.spawned[0]
    assert argv[argv.index("--mode") + 1] == "rollback"
    # Сторож должен получить именно ту версию, на которую возвращаемся.
    assert argv[argv.index("--new") + 1] == git.tags["stable"]


async def test_rollback_noop_when_already_there(service):
    svc, git, systemd, _repo, _s = service
    result = await svc.rollback(RollbackCommand(target=git.head()))
    assert not result.ok and systemd.spawned == []


async def test_versions_merge_git_log_with_outcomes(service):
    svc, git, _systemd, repo, _s = service
    await repo.add(Deployment(note="п", prev_sha="a" * 40, new_sha="c" * 40,
                              phase=DeployPhase.ROLLED_BACK, reason="умер на выдержке"))
    versions = await svc.versions(3)
    assert versions[0].is_current and versions[0].phase is DeployPhase.ROLLED_BACK
    assert versions[0].reason == "умер на выдержке"
    assert versions[1].is_stable


async def test_sync_from_guard_moves_verdict_into_db(service):
    svc, _git, _systemd, repo, settings = service
    deployment = await repo.add(Deployment(note="п", prev_sha="a", new_sha="d",
                                           phase=DeployPhase.APPLYING))
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    settings.update_file.write_text(json.dumps({
        "deployment_id": deployment.id, "phase": "rolled_back",
        "reason": "не поднялся за 90с", "revived_in_s": 12.0,
    }))
    await svc.sync_from_guard()
    saved = await repo.get(deployment.id)
    assert saved.phase is DeployPhase.ROLLED_BACK
    assert saved.reason == "не поднялся за 90с"
    assert saved.finished_at is not None


async def test_mark_stable_moves_the_tag(service):
    svc, git, _systemd, _repo, _s = service
    svc.mark_stable("HEAD")
    assert git.tags["stable"] == git.head()
