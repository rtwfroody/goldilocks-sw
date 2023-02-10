TARGET=/media/$(USER)/CIRCUITPY/
#TARGET=/cygdrive/d
MODULES=$(realpath $(dir $(shell find lib -name __init__.py)))
ONE_FILE_MODULES=$(shell find lib -maxdepth 2 -name '*.py')
FONTS = \
	font/DejaVuSansMono-Bold-18.pcf \
	font/DejaVuSansMono-Bold-12.pcf \
	font/DejaVuSansMono-Bold-8.pcf \
	font/DejaVuSansMono-Bold-24.pcf \
	font/DejaVuSansMono-Bold-30.pcf \
	font/DejaVuSansMono-30.pcf \
	font/DejaVuSansMono-24.pcf \
	font/DejaVuSansMono-18.pcf \
	font/DejaVuSansMono-12.pcf \
	font/DejaVuSansMono-8.pcf

space := $(eval) $(eval)
ONE_FILE_PATHS=$(realpath $(dir $(ONE_FILE_MODULES)))
PYTHONPATH=$(subst $(space),:,$(MODULES) $(ONE_FILE_PATHS))

# Can't use rsync, presumably because circuitpython doesn't implement a proper
# file system.
RSYNC=cp --update

all:	pylint install

install:	install_src install_lib install_font
	sync

install_src:
	$(RSYNC) src/* $(TARGET)

install_lib:
	$(RSYNC) -r $(MODULES) $(ONE_FILE_MODULES) $(TARGET)/lib/

install_font:	$(FONTS)
	mkdir -p $(TARGET)/font; \
	$(RSYNC) -r $(FONTS) $(TARGET)/font/

%-30.bdf:	%.ttf
	otf2bdf -p 30 -l 32_127 -o $@ $< || true

%-24.bdf:	%.ttf
	otf2bdf -p 24 -l 32_127 -o $@ $< || true

%-18.bdf:	%.ttf
	otf2bdf -p 18 -l 32_127 -o $@ $< || true

%-12.bdf:	%.ttf
	otf2bdf -p 12 -l 32_127 -o $@ $< || true

%-8.bdf:	%.ttf
	otf2bdf -p 8 -l 32_127 -o $@ $< || true

%.pcf:	%.bdf
	bdftopcf $< > $@

pylint:
	PYTHONPATH=$(PYTHONPATH) pylint src/*.py
