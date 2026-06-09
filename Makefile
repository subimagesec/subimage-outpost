test: test_lint

test_lint:
	uv run --frozen pre-commit run --all-files --show-diff-on-failure

release:
	@if [ -z "$(VERSION)" ]; then \
		echo "Provide a VERSION like: make release VERSION=1.2.3"; exit 1; \
	fi
	git tag $(VERSION)
	git push origin $(VERSION)
	@echo "Tagged and pushed $(VERSION)"
