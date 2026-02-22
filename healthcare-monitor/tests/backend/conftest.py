"""Pytest configuration for backend unit tests."""
import sys, os
# Ensure the backend app is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../backend'))
