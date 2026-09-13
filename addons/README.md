# Addons for Project Notes

This folder contains optional addon modules that extend the functionality of Project Notes.

The application automatically discovers and loads all addons in this folder at startup.

## How It Works

The addon system uses Python's dynamic import capabilities to:
1. Scan the `addons/` folder for Python modules
2. Import each module and look for addon instances
3. Register addon features (menu items, preferences, etc.)
4. Make functionality available seamlessly in the UI

**No code changes required in the main app to add new addons!**

## Creating Custom Addons

Addons are automatically discovered if they follow this structure:

### 1. Create Your Addon Class

```python
class MyAddon:
    def __init__(self):
        self.name = "My Addon"
        self.version = "1.0.0"
        self.description = "Description of what it does"
        self.preferences_category_name = "My Settings"  # Optional
    
    # Optional: Add menu items to Tools menu
    def register_menu_items(self, menu, main_window):
        """Register addon menu items."""
        menu.addAction("My Action", lambda: self.my_function(main_window))
    
    # Optional: Add preferences/settings UI
    def create_preferences_widget(self, settings, parent=None):
        """Create and return a QWidget with your settings UI."""
        widget = QWidget()
        # ... build your settings UI
        return widget
    
    def save_preferences(self, widget, settings):
        """Save preferences from widget to QSettings."""
        # ... save your settings
        settings.sync()
    
    # Your addon functionality
    def my_function(self, main_window):
        # Access main_window.db, main_window._settings, etc.
        pass

# Create singleton instance - REQUIRED
my_addon = MyAddon()
```

### 2. Save to `addons/` folder

Save your file as `addons/my_addon.py`

### 3. Restart the application

Your addon will be automatically discovered and loaded!

## Addon API

Your addon instance can implement these optional methods:

### Menu Integration
- `register_menu_items(menu, main_window)` - Add items to Tools menu
  - `menu`: QMenu object where you can add actions
  - `main_window`: Reference to MainWindow for accessing app state

### Preferences Integration
- `preferences_category_name` (attribute) - Name shown in preferences sidebar
- `create_preferences_widget(settings, parent)` - Return a QWidget with your UI
- `save_preferences(widget, settings)` - Save settings from widget

### Access to App Components

From `main_window` you can access:
- `main_window.db` - Database connection
- `main_window._settings` - QSettings for persistence
- `main_window.show_preferences()` - Open preferences dialog
- All other MainWindow methods and attributes

## Requirements

- Addon files must be valid Python modules
- Must create a singleton instance at module level
- Instance must implement at least one of:
  - `register_menu_items()` 
  - `create_preferences_widget()` and `save_preferences()`
- File names starting with `_` are ignored

## Example Structure

```
addons/
├── __init__.py
├── README.md
└── my_custom_addon.py      # Your addon
```

## Best Practices

1. **Graceful degradation**: Handle missing dependencies gracefully
2. **Self-contained**: Keep all addon code in one file when possible
3. **Clear naming**: Use descriptive names for menu items and settings
4. **Error handling**: Catch exceptions to avoid breaking the main app
5. **Documentation**: Add docstrings and comments
