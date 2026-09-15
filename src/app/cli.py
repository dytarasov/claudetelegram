"""Командная строка самодопила — то, чем пользуюсь я сам, а не человек в чате.

Человеку хватает /rollback, /versions и /stable в Telegram. Мне нужен способ
выкатить правку из терминала: отредактировал файлы → `cli.py deploy --note …`,
и дальше всё делает сторож. Отдельная точка входа нужна ещё и потому, что
выкатка обязана работать, когда бот лежит и хендлеры недоступны.

    venv/bin/python src/app/cli.py deploy --note "что сделано"
    venv/bin/python src/app/cli.py preflight
    venv/bin/python src/app/cli.py versions
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Запуск файлом, а не пакетом: добавляем src/ в путь, чтобы работал `import app`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.di import build_container  # noqa: E402
from app.domain.dto import DeployCommand, RollbackCommand  # noqa: E402
from app.services.selfupdate import SelfUpdateService  # noqa: E402
from app.services.journal import JournalService  # noqa: E402
from app.services.terms import TermsService  # noqa: E402


async def _service() -> tuple[SelfUpdateService, object]:
    container = build_container()
    return await container.get(SelfUpdateService), container


async def cmd_deploy(args) -> int:
    service, container = await _service()
    try:
        result = await service.deploy(DeployCommand(
            note=args.note, delay_s=args.delay, soak_s=args.soak,
            health_timeout_s=args.health, chat_id=args.chat,
            require_manual_stable=args.manual_stable,
        ))
        if result.preflight:
            for name, ok in result.preflight.checks.items():
                print(f"  {name}: {'прошло' if ok else 'ПРОВАЛ'}")
        print(("ok: " if result.ok else "отказ: ") + result.message)
        for problem in (result.preflight.problems if result.preflight else []):
            print("   " + problem)
        return 0 if result.ok else 1
    finally:
        await container.close()


async def cmd_rollback(args) -> int:
    service, container = await _service()
    try:
        result = await service.rollback(RollbackCommand(
            target=args.target, chat_id=args.chat, reason=args.reason, delay_s=args.delay,
        ))
        print(("откат: " if result.ok else "отказ: ") + result.message)
        return 0 if result.ok else 1
    finally:
        await container.close()


async def cmd_preflight(_args) -> int:
    service, container = await _service()
    try:
        report = await service.preflight()
        for name, ok in report.checks.items():
            print(f"{name}: {'прошло' if ok else 'ПРОВАЛ'}")
        for problem in report.problems:
            print("   " + problem)
        return 0 if report.ok else 1
    finally:
        await container.close()


async def cmd_versions(args) -> int:
    service, container = await _service()
    try:
        for v in await service.versions(args.n):
            marks = " ".join(m for m, on in (("←сейчас", v.is_current),
                                             ("stable", v.is_stable)) if on)
            phase = f" [{v.phase}]" if v.phase else ""
            print(f"{v.short}  {v.when:<16} {v.subject}{phase} {marks}")
        return 0
    finally:
        await container.close()


async def cmd_term(args) -> int:
    """Пополнение живого словаря расшифровки — то, чем я правлю услышанное сам."""
    container = build_container()
    try:
        service = await container.get(TermsService)
        if args.action == "add":
            saved = await service.learn(args.canonical, args.variants)
            print(f"запомнил: {', '.join(t.variant for t in saved)} → {args.canonical}")
        elif args.action == "rm":
            for variant in args.variants:
                print(("убрал: " if await service.forget(variant) else "не было: ") + variant)
        else:
            for canonical, variants in sorted((await service.mapping()).items()):
                print(f"{canonical:15} ← {', '.join(variants)}")
        return 0
    finally:
        await container.close()


async def cmd_ask(args) -> int:
    """Разовый вопрос внешней модели — проверить ключ и посмотреть на ответ."""
    from app.domain.ports import LLMClient

    container = build_container()
    try:
        llm = await container.get(LLMClient)
        if not llm.available():
            print("ключ не задан: добавь LLM_API_KEY в .env")
            return 1
        reply = await llm.complete(" ".join(args.prompt), model=args.model,
                                   max_tokens=args.max_tokens)
        print(reply.text)
        print(f"\n— {reply.model} · {reply.tokens_in}+{reply.tokens_out} токенов "
              f"· ${reply.cost_usd:.5f}")
        return 0
    finally:
        await container.close()


async def cmd_embeddings(args) -> int:
    """Состояние векторной памяти, доиндексация и смена размерности."""
    from app.infrastructure.db.database import Database
    from app.settings import Settings

    container = build_container()
    try:
        journal = await container.get(JournalService)
        if args.action == "status":
            stats = await journal.memory_stats()
            if not stats["available"]:
                print("эмбеддер не настроен: EMBEDDINGS_API_KEY и EMBEDDINGS_MODEL в .env")
                return 1
            print(f"модель:      {stats['model']} (vector({stats['dim']}))")
            print(f"кусков:      {stats['indexed']} с вектором из {stats['total']}")
            print(f"источников:  {stats['sources']} записей, ходов всего {stats['turns']}")
            return 0
        if args.action == "index":
            total = 0
            # Крутим, пока не кончится: один заход берёт ограниченную пачку.
            # Один заход берёт ограниченную пачку: и нарезка, и векторы.
            while (done := await journal.index_pending(args.limit)):
                total += done
                print(f"  +{done}")
            print(f"посчитано векторов: {total}")
            return 0

        # resize: колонка меняет тип, старые векторы теряют смысл — обнуляем.
        db = await container.get(Database)
        async with db.connection() as conn:
            # chunks — рабочая таблица поиска; turns и notes держат старые
            # колонки ради совместимости с откатом (см. миграцию 0008).
            for table in ("chunks", "turns", "notes"):
                await conn.execute(f"UPDATE {table} SET embedding = NULL, embedding_model = NULL")
                await conn.execute(
                    f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({args.dim})")
        print(f"колонки embedding переведены в vector({args.dim}), векторы обнулены")
        print(f"не забудь EMBEDDINGS_DIM={args.dim} в .env, затем `cli.py embeddings index`")
        return 0
    finally:
        await container.close()


async def cmd_stable(args) -> int:
    service, container = await _service()
    try:
        print("stable →", service.mark_stable(args.ref)[:7])
        return 0
    finally:
        await container.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="самодопил бота")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="проверить, закоммитить и перезапуститься под присмотром")
    d.add_argument("--note", required=True, help="что сделано — уйдёт в коммит")
    d.add_argument("--delay", type=int, default=8, help="пауза перед рестартом, сек")
    d.add_argument("--soak", type=int, default=120, help="выдержка после старта, сек")
    d.add_argument("--health", type=int, default=90, help="сколько ждать здорового пульса, сек")
    d.add_argument("--chat", type=int, default=None)
    d.add_argument("--manual-stable", action="store_true",
                   help="не двигать stable автоматически — ждать /stable")
    d.set_defaults(func=cmd_deploy)

    r = sub.add_parser("rollback", help="вернуться на stable / коммит / N шагов назад")
    r.add_argument("target", nargs="?", default="stable")
    r.add_argument("--delay", type=int, default=3)
    r.add_argument("--chat", type=int, default=None)
    r.add_argument("--reason", default="ручной откат")
    r.set_defaults(func=cmd_rollback)

    p = sub.add_parser("preflight", help="только проверки, ничего не менять")
    p.set_defaults(func=cmd_preflight)

    v = sub.add_parser("versions", help="последние версии и их исход")
    v.add_argument("-n", type=int, default=10)
    v.set_defaults(func=cmd_versions)

    a = sub.add_parser("ask", help="разовый вопрос внешней модели")
    a.add_argument("prompt", nargs="+")
    a.add_argument("--model", default=None)
    a.add_argument("--max-tokens", type=int, default=1024)
    a.set_defaults(func=cmd_ask)

    e = sub.add_parser("embeddings", help="векторная память: состояние и индексация")
    e.add_argument("action", choices=["status", "index", "resize"])
    e.add_argument("dim", nargs="?", type=int, default=1536,
                   help="новая размерность для resize")
    e.add_argument("--limit", type=int, default=200)
    e.set_defaults(func=cmd_embeddings)

    t = sub.add_parser("term", help="живой словарь расшифровки голосовых")
    t.add_argument("action", choices=["add", "rm", "list"])
    t.add_argument("canonical", nargs="?", default="", help="как должно быть")
    t.add_argument("variants", nargs="*", help="как услышалось (можно несколько)")
    t.set_defaults(func=cmd_term)

    s = sub.add_parser("stable", help="пометить версию стабильной")
    s.add_argument("ref", nargs="?", default="HEAD")
    s.set_defaults(func=cmd_stable)

    args = ap.parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
