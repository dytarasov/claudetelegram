# Одна кнопка. Полная установка на чистой машине:
#
#   sudo make install
#
# Дальше боту нужны только токен и белый список — install.sh подскажет шаги.
ROOT := $(shell pwd)
PY   := $(ROOT)/venv/bin/python

.PHONY: bootstrap install db test run preflight deploy rollback help

help:
	@echo "make bootstrap TOKEN=<токен>   базовая установка + донастройка через Телеграм (нужен sudo)"
	@echo "make install    полная установка целиком на сервере: venv, зависимости, Postgres+pgvector, служба (нужен sudo)"
	@echo "make install ARGS=--cloud-stt   то же, но без локального whisper: STT в облаке (Groq)"
	@echo "make db         только провижининг БД и запись DSN в .env (нужен sudo)"
	@echo "make test       прогнать тесты"
	@echo "make run        запустить бота вручную (без systemd)"
	@echo "make preflight  проверка перед выкаткой: компиляция, импорт, настройки, тесты"
	@echo "make deploy NOTE='что сделано' CHAT=<id>   выкатка с самооткатом"
	@echo "make rollback REF=stable                   вернуться на версию"

bootstrap:
	TOKEN="$(TOKEN)" ./scripts/bootstrap.sh

install:
	./scripts/install.sh $(ARGS)

db:
	./scripts/setup_db.sh

test:
	$(PY) -m pytest -q

run:
	$(PY) src/bot.py

preflight:
	$(PY) src/app/cli.py preflight

deploy:
	$(PY) src/app/cli.py deploy --note "$(NOTE)" --chat "$(CHAT)"

rollback:
	$(PY) src/app/cli.py rollback "$(REF)"
