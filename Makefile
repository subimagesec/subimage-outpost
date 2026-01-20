test: test_lint

test_lint:
	uv run --frozen pre-commit run --all-files --show-diff-on-failure