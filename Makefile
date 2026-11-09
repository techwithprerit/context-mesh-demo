.PHONY: stage up down logs local local-down status demo rehearse preflight watch reset clean

# the repo venv if there is one (Homebrew/system Python refuses `pip install`), else whatever python3 is on PATH
PY ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

up:            ## docker: build and start the whole mesh
	mkdir -p data && docker compose up --build -d && sleep 25 && $(MAKE) status

down:          ## docker: stop and remove (keeps ./data)
	docker compose down

logs:
	docker compose logs -f --tail=50

local:         ## no docker: 7 python processes, file bus
	scripts/run_local.sh up

local-down:
	scripts/run_local.sh down

status:
	BUS_URL=redis://localhost:6379/0 $(PY) -m mesh.cli status

demo:          ## the run of show, one Enter per beat
	BUS_URL=redis://localhost:6379/0 $(PY) -m mesh.cli demo

rehearse:      ## the whole run of show, unattended (~2.5 min)
	BUS_URL=redis://localhost:6379/0 $(PY) -m mesh.cli rehearse

watch:         ## pane B: the bus, live
	BUS_URL=redis://localhost:6379/0 $(PY) -m mesh.cli watch --all

preflight:     ## go / no-go before walking on stage
	BUS_URL=redis://localhost:6379/0 $(PY) -m mesh.cli preflight

reset:         ## clean slate between rehearsals: chaos, freeze, deploys, the telemetry windows, and the nodes' memory
	$(PY) -m mesh.cli chaos reset; rm -f data/deploys.json data/freeze.json
	touch data/wipe && sleep 2   # the estate empties its stores within a tick, before the nodes come back
	docker compose restart graph metrics logs traces topology policy 2>/dev/null || scripts/run_local.sh restart-nodes
	@echo "wait ~30 s, then: make preflight"

stage:         ## T-10 in one command: Docker running, stack up, clean slate, go/no-go
	@docker info >/dev/null 2>&1 || { echo "starting Docker Desktop"; open -a Docker; until docker info >/dev/null 2>&1; do sleep 2; done; }
	mkdir -p data && docker compose up -d
	$(MAKE) reset
	sleep 30
	$(MAKE) preflight

clean: down
	rm -rf data/*.db* data/bus.jsonl data/*.json data/logs data/pids data/wipe
