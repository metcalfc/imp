# imp -- lend a Fly Sprite your Claude Max subscription. See README.md.
#
#   make install                      -> ~/.local/bin
#   make install DIR=/usr/local/bin   -> anywhere else
#   make install-auth                 -> imp-auth, if you want it
#
# No build step; these are scripts. The Makefile exists so the install path is
# written down once instead of in every README.

DIR ?= $(HOME)/.local/bin
BINS = imp imp-proxy
AUTH = imp-auth

.DEFAULT_GOAL := help
.PHONY: help install install-auth uninstall

help:
	@echo 'make install [DIR=path]      install $(BINS) (default: $(DIR))'
	@echo 'make install-auth [DIR=path] install $(AUTH) as well -- opt in'
	@echo 'make uninstall [DIR=path]    remove all three again'

install: $(BINS)
	@$(MAKE) --no-print-directory _put FILES='$(BINS)'

# Separate, and never a dependency of install. imp-auth mints a long-lived
# credential and can write one straight into a sprite's settings.json, which
# is a thing to reach for deliberately rather than to find on your PATH
# because you installed something else. Once a year is its natural frequency;
# the clone is a fine place for it.
install-auth: $(AUTH)
	@$(MAKE) --no-print-directory _put FILES='$(AUTH)'

.PHONY: _put
_put:
	install -d $(DIR)
	install -m 755 $(FILES) $(DIR)
# imp looks for imp-proxy next to itself before consulting PATH, so the two
# stay a matched pair even with an older copy installed elsewhere. Worth
# saying when DIR is not somewhere the shell will look.
	@case ":$$PATH:" in \
	  *":$(DIR):"*) ;; \
	  *) echo; echo 'note: $(DIR) is not on your PATH' ;; \
	esac

# Everything install and install-auth could have put there. rm -f does not
# mind what is absent, and leaving imp-auth behind after `make uninstall`
# would be the surprising half of a symmetry.
uninstall:
	cd $(DIR) && rm -f $(BINS) $(AUTH)
