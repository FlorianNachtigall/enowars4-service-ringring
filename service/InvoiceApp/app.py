from flask import Flask, render_template, request, redirect, make_response, jsonify
from flask_table import Table, Col
from urllib.parse import urlparse
from pathlib import Path
import logging.config
import secrets
import yaml
import json
import sys

ACCOUNT = 5
PAYMENT_ON_ACCOUNT = 'room-bill'
PAYMENT_SETTLED = 'cash'
OUTSTANDING_INVOICES = 'accounting/outstanding-invoices.log'
SETTLED_INVOICES = 'accounting/settled-invoices.log'

app = Flask(__name__)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(sys.stdout))


class InvoiceFilter:
    def __init__(self, payment_method, invoice_status):
        self.payment_method = payment_method
        self.invoice_status = invoice_status

    def get_invoice_status(self, payment_method):
        if payment_method == 'cash' or payment_method == 'debit':
            return 'settled'
        else:
            return 'outstanding'

    def filter(self, logRecord):
        return logRecord.levelno == ACCOUNT and self.get_invoice_status(self.payment_method) == self.invoice_status


@app.route('/')
def home():
    guest_name = request.args.get('name')
    if not guest_name:
        logger.warning(
            f"Abort getting invoice overview - mandatory parameter guest name '{guest_name}' is not set (HTTP 404).")
        return jsonify(success=False), 404

    log_level = request.args.get('log-level', 'DEBUG')
    controller = get_invoice_controller(log_level=log_level)
    controller.info(log_level)

    controller.info(f"Generating invoice overview for guest '{guest_name}'...")
    guest_invoices = get_invoice_items(guest_name=guest_name, include_settled=False)
    return jsonify(invoices=guest_invoices, success=True)


@app.route('/add', methods=['POST'])
def add_to_bill():
    guest_name = request.form.get('name')
    if not guest_name:
        return param_error('name')

    invoice_item = request.form.get('item')
    if not invoice_item:
        return param_error('item')

    payment_method = request.form.get('payment-method', PAYMENT_ON_ACCOUNT)
    note = request.form.get('note', '')

    if not validate_invoice(guest_name, invoice_item):
        logger.warning(
            f"Aborting invoice accounting - invoice parameters guest name '{guest_name}' and item '{invoice_item}' are not valid (HTTP 404).")
        return jsonify(success=False), 400

    amount = get_price(invoice_item)
    invoice_number = get_invoice_number()
    invoice = {
        'invoice_number': invoice_number,
        'item': invoice_item,
        'guest_name': guest_name,
        'amount': amount,
        'note': note
    }

    controller = get_invoice_controller(payment_method=payment_method)
    controller.account(f'invoice #{invoice_number} accounted', extra=invoice)
    return jsonify(success=True, invoice_number=invoice_number)


@app.route('/storno', methods=['POST'])
def storno():
    controller = get_invoice_controller()
    invoice_number = request.form.get('number')
    if not invoice_number:
        return param_error('number')

    for invoice in accounted_invoices():
        if invoice['invoice_number'] == invoice_number:
            # creating storno invoice number
            invoice['invoice_number'] = get_invoice_number()
            invoice['amount'] = float(invoice['amount']) * -1
            invoice['guest_name'] = invoice.pop('name')
            invoice.pop('time')
            controller.account(f"cancelling invoice #{invoice_number} (negative booking #{invoice['invoice_number']})",
                               extra=invoice)
            return jsonify(success=True)

    logger.warning(f"cancelling invoice failed - no invoice found for invoice number '{invoice_number}' (HTTP 404).")
    return jsonify(success=False), 404


@app.route('/request-bill')
def request_bill():
    guest_name = request.args.get('name')
    if not guest_name:
        return param_error('name')

    logger.info(f"Requesting bill for guest '{guest_name}'...")

    bill = []
    items = []
    total = 0.0
    for invoice in accounted_invoices(guest_name):
        bill.append(invoice)
        items.append(invoice['item'])
        total += float(invoice['amount'])

    settle_bill_invoices(bill)
    return jsonify(total=total, items=items, success=True)


@app.route('/invoice_details')
def invoice_details():
    invoice_number = request.args.get('invoice_number')
    if not invoice_number:
        return param_error('invoice_number')

    guest_name = request.args.get('guest_name')
    if not guest_name:
        return param_error('guest_name')

    logger.info(f"Requesting invoice '{invoice_number}'...")

    invoice = get_invoice_by_number(invoice_number, guest_name)
    logger.info(f"retruning invoice information: {invoice}")

    return jsonify(invoice=invoice, success=True)


def settle_bill_invoices(bill):
    controller = get_invoice_controller(payment_method=PAYMENT_SETTLED)
    for invoice in bill:
        invoice['guest_name'] = invoice.pop('name')
        invoice.pop('time')
        controller.account(f"invoice #{invoice['invoice_number']} settled", extra=invoice)

    invoice_numbers = [invoice['invoice_number'] for invoice in bill]
    with open(OUTSTANDING_INVOICES, 'r') as f:
        journal = f.readlines()

    with open(OUTSTANDING_INVOICES, 'w') as f:
        for invoice in journal:
            try:
                if json.loads(invoice)['invoice_number'] not in invoice_numbers:
                    f.write(invoice)
            except Exception as e:
                logger.error(f"Error settling bill for invoice '{invoice}': {e}")


def validate_invoice(guest_name, invoice_item):
    return guest_name and invoice_item


def get_price(item):
    price_sheet = {
        'alarm': 1.50,
        'pizza': 6.00,
        'bred': 2.00,
        'fish': 15.00,
        'wine': 4.00,
        'room-service-food': 9.99,
        'reception': 0.0,
        'extra-cleaning': 20.0
    }
    return price_sheet.get(item, 0)


def get_invoice_number():
    return secrets.randbits(32)


def accounted_invoices(guest_name=None, file_path=OUTSTANDING_INVOICES):
    if not Path(file_path).is_file():
        return

    with open(file_path) as journal:
        for entry in journal:
            try:
                invoice = json.loads(entry)
            except Exception as e:
                logger.error(f"Error reading invoice '{entry}': {e}")
                continue
            if not guest_name or invoice['name'] == guest_name:
                yield invoice


def get_invoice_by_number(invoice_number, guest_name):
    for invoice in accounted_invoices(guest_name):
        if invoice['invoice_number'] == invoice_number:
            return invoice
    return {}


def get_invoice_items(guest_name=None, include_settled=False):
    invoices = list(accounted_invoices(guest_name=guest_name))
    if include_settled:
        invoices.extend(
            accounted_invoices(guest_name=guest_name, file_path=SETTLED_INVOICES))
    logger.info(f"Returning invoices for {guest_name}: {invoices}")
    for invoice in invoices:
        invoice.pop('invoice_number')
        invoice.pop('note')
    return invoices


def get_invoice_controller(payment_method=PAYMENT_ON_ACCOUNT, log_level='ACCOUNT'):
    with open('logger-config.yml', 'r') as yaml_file:
        config = yaml_file.read().format(payment_method=payment_method, level=log_level)
        config = yaml.load(config, Loader=yaml.Loader)
        logging.addLevelName(ACCOUNT, 'ACCOUNT')
        logging.config.dictConfig(config)
        logging.Logger.account = account
        logger = logging.getLogger('invoice_controller')
        logger.debug('invoice-controller logger started.')
    return logger


def account(self, msg, *args, **kwargs):
    if self.isEnabledFor(ACCOUNT):
        self._log(ACCOUNT, msg, args, **kwargs)


def start_app(host, threaded=False):
    app.run(port=7354, host=host, threaded=threaded)


def param_error(name):
    return jsonify(success=False, message=f"missing parameter {name}"), 400


if __name__ == '__main__':
    start_app(host='0.0.0.0')
