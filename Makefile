# Thin wrapper around install.sh, for machines that have make.
# install.sh is the real entry point and needs nothing but bash and uv.

.PHONY: help install uninstall dev test lint fmt

help:
	@./install.sh help

install uninstall dev test lint fmt:
	@./install.sh $@
