# imp -- lend a Fly Sprite your Claude Max subscription. See README.md.
#
#   make install                      -> ~/.local/bin
#   make install DIR=/usr/local/bin   -> anywhere else
#
# No build step; these are two scripts. The Makefile exists so the install
# path is written down once instead of in every README.

DIR ?= $(HOME)/.local/bin
BINS = imp imp-proxy

.DEFAULT_GOAL := help
.PHONY: help install uninstall

help:
	@echo 'make install [DIR=path]   install $(BINS) (default: $(DIR))'
	@echo 'make uninstall [DIR=path] remove them again'

install:
	install -d $(DIR)
	install -m 755 $(BINS) $(DIR)
# imp looks for imp-proxy next to itself before consulting PATH, so the two
# stay a matched pair even with an older copy installed elsewhere. Worth
# saying when DIR is not somewhere the shell will look.
	@case ":$$PATH:" in \
	  *":$(DIR):"*) ;; \
	  *) echo; echo 'note: $(DIR) is not on your PATH' ;; \
	esac

uninstall:
	cd $(DIR) && rm -f $(BINS)
