.PHONY: all init build logs shell run stop help

all: help

uid := $(shell id -u)
gid := $(shell id -g)

PROJECT_NAME := radionow
DOCKER_NETWORK_NAME ?= breathnet

build:
	@echo "Building radionow image..."
	uid=$(uid) gid=$(gid) docker compose -p $(PROJECT_NAME) build

init:
	@echo "Ensuring local data directories exist..."
	@mkdir -p audio voices
	@echo "Ensuring Docker network exists: $(DOCKER_NETWORK_NAME)"
	@docker network inspect $(DOCKER_NETWORK_NAME) >/dev/null 2>&1 || docker network create $(DOCKER_NETWORK_NAME)

logs:
	@docker logs radionow

shell:
	@docker exec -it radionow /bin/bash

run:
	@echo "Starting radionow..."
	$(MAKE) init
	$(MAKE) stop
	@if [ -z "$$(docker images -q radionow)" ]; then \
		echo "Image not found. Building..."; \
		$(MAKE) build; \
	fi
	@if [ -z "$$RADIONOW_MONGO_URL" ]; then \
		echo "RADIONOW_MONGO_URL not set; starting with local MongoDB (profile: localdb)"; \
		docker compose -p $(PROJECT_NAME) --profile localdb up -d; \
	else \
		echo "RADIONOW_MONGO_URL set; using external MongoDB and starting app only"; \
		docker compose -p $(PROJECT_NAME) up -d; \
	fi

stop:
	@docker compose -p $(PROJECT_NAME) down

help:
	@echo '=========================='
	@echo '  RADIONOW - MAKEFILE  '
	@echo '=========================='
	@echo ''
	@echo 'Build Commands:'
	@echo '  build               - Build production image'
	@echo ''
	@echo 'Run Commands:'
	@echo '  run                 - Run in production mode'
	@echo ''
	@echo 'Logging Commands:'
	@echo '  logs                - Show radionow logs'
	@echo ''
	@echo 'Shell Access:'
	@echo '  shell               - Access radionow container shell'
	@echo ''
	@echo 'Maintenance Commands:'
	@echo '  stop                - Stop the radionow container'
	@echo ''
	@echo 'Note: Deployment is handled by the root Makefile'
	@echo '=========================='
