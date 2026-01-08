"""Notifications module for sending alerts"""
from .email_notifier import EmailNotifier, send_signal_email

__all__ = ['EmailNotifier', 'send_signal_email']
