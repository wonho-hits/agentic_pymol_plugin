PLUGIN_NAME := agentic_pymol_plugin
DIST_DIR    := dist
BUILD_DIR   := $(DIST_DIR)/build
ZIP_PATH    := $(DIST_DIR)/$(PLUGIN_NAME).zip

PLUGIN_FILES := __init__.py config.py plugin_side

.PHONY: plugin clean test

plugin: $(ZIP_PATH)

$(ZIP_PATH): $(PLUGIN_FILES)
	@rm -rf $(BUILD_DIR)
	@mkdir -p $(BUILD_DIR)/$(PLUGIN_NAME)
	@cp __init__.py $(BUILD_DIR)/$(PLUGIN_NAME)/
	@cp config.py   $(BUILD_DIR)/$(PLUGIN_NAME)/
	@cp -R plugin_side $(BUILD_DIR)/$(PLUGIN_NAME)/
	@find $(BUILD_DIR) -type d -name __pycache__ -exec rm -rf {} +
	@find $(BUILD_DIR) -type f -name '*.pyc'      -delete
	@find $(BUILD_DIR) -type f -name '.DS_Store'  -delete
	@rm -f $(ZIP_PATH)
	@cd $(BUILD_DIR) && zip -r ../$(PLUGIN_NAME).zip $(PLUGIN_NAME) >/dev/null
	@echo "Built $(ZIP_PATH)"

clean:
	@rm -rf $(DIST_DIR)

test:
	@pytest
