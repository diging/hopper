# Why these files are empty

The `logged_in.txt` and `logged_out.txt` files are intentionally empty. They override the default django-allauth message templates to suppress the built-in "Successfully signed in" and "Successfully signed out" toast notifications.

If allauth doesn't find these overrides, it falls back to its own templates which display flash messages on login/logout. Keeping these files empty effectively disables those messages.
