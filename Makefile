test: test_lint

test_lint:
	uv run --frozen pre-commit run --all-files --show-diff-on-failure

release:
	@if [ -z "$(VERSION)" ]; then \
		echo "Provide a VERSION like: make release VERSION=1.2.3"; exit 1; \
	fi
	uv version $(VERSION)
	git add pyproject.toml uv.lock
	git commit -m "chore: release $(VERSION)"
	git tag $(VERSION)
	git push origin HEAD $(VERSION)
	@echo "Released $(VERSION)"
