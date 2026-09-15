"""Самодопил: проверить, закоммитить, перезапуститься под присмотром, откатиться.

Устройство и — главное — почему именно так.

  1. Версия = коммит. Метка stable = «сюда возвращаться». Метка bad/<дата> —
     «здесь сломалось, но не потеряно». Ветки не заводим: линейная история
     читается глазами в час ночи, когда бот лежит, а мердж-граф — нет.

  2. Перед выкаткой — preflight: компиляция, импорт всех модулей, юнит-тесты и
     проверка настроек. Не прошло — не коммитим вообще ничего, человек даже не
     замечает. Дешевле поймать здесь, чем сторожем через полторы минуты.

  3. Сторож (scripts/guard.sh) запускается ОТДЕЛЬНЫМ systemd-юнитом. Это не
     украшение: `systemctl restart claude-tg` убивает весь cgroup сервиса, и
     сторож, запущенный обычным Popen, умер бы вместе с ботом ровно в тот
     момент, когда он единственный, кто может нас спасти.

  4. Канал общения со сторожем — файл run/update.json, а не БД. Сторож должен
     работать, когда бот не поднялся и до Postgres никто не достучался. БД
     хранит историю (кто, что, чем кончилось), файл — текущую операцию.
     Синхронизирует их sync_from_guard(), который зовётся на каждом ударе пульса.

  5. Здоровьем считается не «процесс запустился», а пульс с тремя флагами:
     ready + telegram_ok + claude_alive (см. services/health.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from ..domain.dto import (DeployCommand, DeployResult, PreflightReport,
                          RollbackCommand, VersionView)
from ..domain.enums import DeployPhase, TriggerKind
from ..domain.models import Deployment
from ..domain.ports import DeploymentRepository, ServiceControl, VersionControl
from ..settings import Settings

from . import events

log = logging.getLogger(__name__)

# Что попадает в коммит. Список явный, а не «всё подряд»: секреты и рантайм не
# должны уехать в историю даже при ошибке в .gitignore.
TRACKED_PATHS = ["src", "scripts", "deploy", "tests", "README.md", "pytest.ini",
                 "CLAUDE.md",
                 "requirements.txt", ".env.example", ".gitignore"]

# Модули, которые обязаны импортироваться. Если хоть один не импортируется —
# бот не поднимется, и это ловится за секунду вместо полутора минут сторожем.
IMPORT_TARGETS = ["app.settings", "app.domain.ports", "app.services.conversation",
                  "app.services.selfupdate", "app.services.health",
                  "app.presentation.routers", "app.di", "app.main"]


class SelfUpdateService:
    def __init__(
        self,
        settings: Settings,
        git: VersionControl,
        systemd: ServiceControl,
        deployments: DeploymentRepository,
    ) -> None:
        self._s = settings
        self._git = git
        self._systemd = systemd
        self._repo = deployments

    # ------------------------------------------------------------------ #
    #  проверки
    # ------------------------------------------------------------------ #
    async def preflight(self) -> PreflightReport:
        """Всё, что можно проверить, не трогая работающего бота."""
        problems: list[str] = []
        checks: dict[str, bool] = {}
        python = str(self._s.venv_python)
        src = str(self._s.src_dir)

        files = sorted(str(p) for p in self._s.src_dir.rglob("*.py"))
        ok, detail = await self._run(python, "-m", "py_compile", *files)
        checks["компиляция"] = ok
        if not ok:
            problems.append(f"компиляция: {detail}")
            return PreflightReport(problems=problems, checks=checks)  # дальше бессмысленно

        ok, detail = await self._run(python, "-c", "import " + ", ".join(IMPORT_TARGETS), cwd=src)
        checks["импорт"] = ok
        if not ok:
            problems.append(f"импорт: {detail}")

        ok, detail = await self._run(
            python, "-c",
            "import sys; sys.path.insert(0, '.'); from app.settings import Settings; "
            "p = Settings().problems(); sys.exit('; '.join(p) if p else 0)",
            cwd=src,
        )
        checks["настройки"] = ok
        if not ok:
            problems.append(f"настройки: {detail}")

        if (self._s.root / "tests").exists():
            ok, detail = await self._run(python, "-m", "pytest", "-q", "--no-header",
                                         cwd=str(self._s.root), timeout=300)
            checks["тесты"] = ok
            if not ok:
                problems.append(f"тесты: {detail}")

        return PreflightReport(problems=problems, checks=checks)

    @staticmethod
    async def _run(*argv: str, cwd: str | None = None, timeout: float = 180) -> tuple[bool, str]:
        """Запустить проверку. Возвращает (прошла ли, последняя внятная строка)."""
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/local/bin:/usr/bin:/bin",
                 "HOME": "/root"},
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return False, f"не уложилось в {timeout:.0f}с"
        lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
        return proc.returncode == 0, (lines[-1][:300] if lines else f"код {proc.returncode}")

    # ------------------------------------------------------------------ #
    #  выкатка
    # ------------------------------------------------------------------ #
    async def deploy(self, cmd: DeployCommand) -> DeployResult:
        in_flight = await self._safe_in_flight()
        if in_flight is not None:
            events.deploy_blocked(f"уже едет выкатка #{in_flight.id} ({in_flight.phase})")
            return DeployResult(
                ok=False,
                message=f"уже едет выкатка #{in_flight.id} ({in_flight.phase}) — дождись итога",
            )

        report = await self.preflight()
        if not report.ok:
            events.deploy_blocked("preflight: " + "; ".join(report.problems)[:150])
            return DeployResult(ok=False, preflight=report,
                                message="preflight не прошёл, код остался прежним")

        prev = self._git.head()
        changes = [path for status, path in self._git.dirty()]
        if not changes:
            return DeployResult(ok=False, message="нечего выкатывать: рабочее дерево чистое")

        new = self._git.commit(f"selfupdate: {cmd.note}", TRACKED_PATHS)
        deployment = Deployment(
            note=cmd.note, prev_sha=prev, new_sha=new, phase=DeployPhase.PENDING,
            trigger=cmd.trigger, files=changes, chat_id=cmd.chat_id,
            started_at=datetime.now(),
        )
        deployment = await self._save(deployment)

        self._write_marker({
            "mode": "deploy",
            "phase": DeployPhase.PENDING,
            "deployment_id": deployment.id,
            "note": cmd.note,
            "prev": prev, "new": new,
            "stable": self._git.tag_sha(self._s.stable_tag),
            "chat_id": cmd.chat_id,
            "files": changes,
            "require_manual_stable": cmd.require_manual_stable,
            "announced": False,
            "started": datetime.now().isoformat(timespec="seconds"),
        })
        events.deploy_started(cmd.note, prev, new, len(changes))
        self._spawn_guard(mode="deploy", prev=prev, new=new, cmd=cmd)
        return DeployResult(
            ok=True, prev_sha=prev, new_sha=new, files=changes, preflight=report,
            deployment_id=deployment.id,
            message=(f"{self._git.short(prev)} → {self._git.short(new)}, "
                     f"{len(changes)} файл(ов); рестарт через {cmd.delay_s}с, "
                     f"выдержка {cmd.soak_s}с"),
        )

    async def rollback(self, cmd: RollbackCommand) -> DeployResult:
        target = self._git.resolve(cmd.target)
        current = self._git.head()
        if target == current:
            return DeployResult(ok=False, message=f"уже на {self._git.short(target)}")

        events.rollback_started(target, self._git.subject(target))
        deployment = await self._save(Deployment(
            note=cmd.reason, prev_sha=current, new_sha=target,
            phase=DeployPhase.ROLLING_BACK, trigger=cmd.trigger,
            chat_id=cmd.chat_id, started_at=datetime.now(),
        ))
        self._write_marker({
            "mode": "rollback",
            "phase": DeployPhase.ROLLING_BACK,
            "deployment_id": deployment.id,
            "note": cmd.reason,
            "prev": current, "new": target,
            "stable": self._git.tag_sha(self._s.stable_tag),
            "chat_id": cmd.chat_id,
            "announced": False,
            "started": datetime.now().isoformat(timespec="seconds"),
        })
        self._spawn_guard(
            mode="rollback", prev=target, new=target,
            cmd=DeployCommand(note=cmd.reason, delay_s=cmd.delay_s, soak_s=0,
                              health_timeout_s=self._s.deploy_health_timeout_s,
                              chat_id=cmd.chat_id),
        )
        return DeployResult(ok=True, prev_sha=current, new_sha=target,
                            deployment_id=deployment.id,
                            message=f"откат на {self._git.short(target)} — "
                                    f"{self._git.subject(target)}")

    def mark_stable(self, ref: str = "HEAD") -> str:
        sha = self._git.resolve(ref)
        self._git.tag(self._s.stable_tag, sha)
        events.stable_marked(sha)
        return sha

    # ------------------------------------------------------------------ #
    #  чтение состояния
    # ------------------------------------------------------------------ #
    async def versions(self, limit: int = 10) -> list[VersionView]:
        """git знает, что коммит есть; БД знает, чем он кончился. Склеиваем."""
        stable = self._git.tag_sha(self._s.stable_tag)
        current = self._git.head()
        views: list[VersionView] = []
        for item in self._git.log(limit):
            deployment = await self._safe_by_sha(item["sha"])
            views.append(VersionView(
                sha=item["sha"], short=item["short"], subject=item["subject"],
                when=item["when"],
                is_current=item["sha"] == current,
                is_stable=item["sha"] == stable,
                phase=deployment.phase if deployment else None,
                reason=deployment.reason if deployment else None,
            ))
        return views

    def read_marker(self) -> dict | None:
        try:
            return json.loads(self._s.update_file.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def mark_announced(self) -> None:
        marker = self.read_marker()
        if marker is not None:
            marker["announced"] = True
            self._write_marker(marker)

    async def sync_from_guard(self) -> None:
        """Перенести вердикт сторожа из файла в БД.

        Зовётся на каждом ударе пульса. Дёшево: чтение файла и запись в БД
        только когда фаза действительно изменилась.
        """
        marker = self.read_marker()
        if not marker or not marker.get("deployment_id"):
            return
        try:
            phase = DeployPhase(marker.get("phase", ""))
        except ValueError:
            return
        deployment = await self._safe_get(int(marker["deployment_id"]))
        if deployment is None or deployment.phase is phase:
            return
        deployment.phase = phase
        deployment.reason = marker.get("reason")
        deployment.revived_in_s = marker.get("revived_in_s")
        if phase in (DeployPhase.OK, DeployPhase.ROLLED_BACK, DeployPhase.FAILED):
            deployment.finished_at = datetime.now()
        events.deploy_verdict(str(phase), deployment.note, deployment.reason)
        try:
            await self._repo.update(deployment)
        except Exception:  # noqa: BLE001
            log.warning("не смог записать вердикт сторожа в БД", exc_info=True)

    # ------------------------------------------------------------------ #
    #  внутреннее
    # ------------------------------------------------------------------ #
    def _write_marker(self, data: dict) -> None:
        self._s.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {k: (str(v) if isinstance(v, DeployPhase) else v) for k, v in data.items()}
        tmp = self._s.update_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        tmp.replace(self._s.update_file)

    def _spawn_guard(self, *, mode: str, prev: str, new: str, cmd: DeployCommand) -> None:
        self._systemd.spawn_detached("claude-tg-guard", [
            "/bin/bash", str(self._s.guard_script),
            "--mode", mode,
            "--prev", prev,
            "--new", new,
            "--delay", str(cmd.delay_s),
            "--soak", str(cmd.soak_s),
            "--health", str(cmd.health_timeout_s),
            "--unit", self._s.service_unit,
            "--stable-tag", self._s.stable_tag,
            "--manual-stable", "1" if cmd.require_manual_stable else "0",
        ])

    # БД не должна ронять самодопил: без истории жить можно, без отката — нет.
    async def _save(self, deployment: Deployment) -> Deployment:
        try:
            return await self._repo.add(deployment)
        except Exception:  # noqa: BLE001
            log.warning("история выкаток недоступна, продолжаю без неё", exc_info=True)
            return deployment

    async def _safe_in_flight(self) -> Deployment | None:
        try:
            return await self._repo.in_flight()
        except Exception:  # noqa: BLE001
            return None

    async def _safe_by_sha(self, sha: str) -> Deployment | None:
        try:
            return await self._repo.by_sha(sha)
        except Exception:  # noqa: BLE001
            return None

    async def _safe_get(self, deployment_id: int) -> Deployment | None:
        try:
            return await self._repo.get(deployment_id)
        except Exception:  # noqa: BLE001
            return None
