# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
import logging

from odoo import api, fields, models, _
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # link with the sales order
    # YTI FIXME: The auto_join seems useless
    sale_order_id = fields.Many2one('sale.order', string='Sales Order', auto_join=True)

    # --------------------------------------------------
    # Sale management
    # --------------------------------------------------

    @api.model
    def form_feedback(self, data, acquirer_name):
        """ Override to confirm the sales order, if defined, and if the transaction
        is done. """
        tx = None
        res = super(PaymentTransaction, self).form_feedback(data, acquirer_name)

        # fetch the tx
        tx_find_method_name = '_%s_form_get_tx_from_data' % acquirer_name
        if hasattr(self, tx_find_method_name):
            tx = getattr(self, tx_find_method_name)(data)
        _logger.info('<%s> transaction processed: tx ref:%s, tx amount: %s', acquirer_name, tx.reference if tx else 'n/a', tx.amount if tx else 'n/a')

        if tx:
            # Auto-confirm SO if necessary
            tx._confirm_so()

        return res

    def _confirm_so(self):
        for tx in self:
            # check tx state, confirm the potential SO
            if not tx.sale_order_id or tx.sale_order_id.state not in ['draft', 'sent']:
                # _logger.warning('<%s> transaction incorrect sale order %s (ID %s, state %s)', tx.acquirer_id.provider, tx.sale_order_id.name, tx.sale_order_id.id, tx.sale_order_id.state)
                continue
            if not float_compare(tx.amount, tx.sale_order_id.amount_total, 2) == 0:
                _logger.warning('<%s> transaction MISMATCH for order %s (ID %s)', tx.acquirer_id.provider, tx.sale_order_id.name, tx.sale_order_id.id)
                continue

            if tx.state == 'authorized' and tx.acquirer_id.capture_manually:
                _logger.info('<%s> transaction authorized, auto-confirming order %s (ID %s)', tx.acquirer_id.provider, tx.sale_order_id.name, tx.sale_order_id.id)
                tx.sale_order_id.with_context(send_email=True).action_confirm()

            if tx.state == 'done':
                _logger.info('<%s> transaction completed, auto-confirming order %s (ID %s)', tx.acquirer_id.provider, tx.sale_order_id.name, tx.sale_order_id.id)
                tx.sale_order_id.with_context(send_email=True).action_confirm()
                tx._generate_and_pay_invoice()
            elif tx.state not in ['cancel', 'error'] and tx.sale_order_id.state == 'draft':
                _logger.info('<%s> transaction pending/to confirm manually, sending quote email for order %s (ID %s)', tx.acquirer_id.provider, tx.sale_order_id.name, tx.sale_order_id.id)
                tx.sale_order_id.force_quotation_send()
            else:
                _logger.warning('<%s> transaction MISMATCH for order %s (ID %s)', tx.acquirer_id.provider, tx.sale_order_id.name, tx.sale_order_id.id)

    def _generate_and_pay_invoice(self):
        self.sale_order_id._force_lines_to_invoice_policy_order()

        # force company to ensure journals/accounts etc. are correct
        # company_id needed for default_get on account.journal
        # force_company needed for company_dependent fields
        ctx_company = {'company_id': self.sale_order_id.company_id.id,
                       'force_company': self.sale_order_id.company_id.id}
        created_invoice = self.sale_order_id.with_context(**ctx_company).action_invoice_create()
        created_invoice = self.env['account.invoice'].browse(created_invoice).with_context(**ctx_company)

        if created_invoice:
            _logger.info('<%s> transaction completed, auto-generated invoice %s (ID %s) for %s (ID %s)',
                         self.acquirer_id.provider, created_invoice.name, created_invoice.id, self.sale_order_id.name, self.sale_order_id.id)

            created_invoice.action_invoice_open()
            if not self.acquirer_id.journal_id:
                default_journal = self.env['account.journal'].search([('type', '=', 'bank')], limit=1)
                if not default_journal:
                    _logger.warning('<%s> transaction completed, could not auto-generate payment for %s (ID %s) (no journal set on acquirer)',
                                    self.acquirer_id.provider, self.sale_order_id.name, self.sale_order_id.id)
                self.acquirer_id.journal_id = default_journal
                created_invoice.pay_and_reconcile(self.acquirer_id.journal_id, pay_amount=created_invoice.amount_total)
                if created_invoice.payment_ids:
                    created_invoice.payment_ids[0].payment_transaction_id = self
        else:
            _logger.warning('<%s> transaction completed, could not auto-generate invoice for %s (ID %s)',
                            self.acquirer_id.provider, self.sale_order_id.name, self.sale_order_id.id)

    # --------------------------------------------------
    # Tools for payment
    # --------------------------------------------------

    def confirm_sale_token(self):
        """ Confirm a transaction token and call SO confirmation if it is a success.

        :return: True if success; error string otherwise """
        self.ensure_one()
        if self.payment_token_id and self.partner_id == self.sale_order_id.partner_id:
            try:
                s2s_result = self.s2s_do_transaction()
            except Exception as e:
                _logger.warning(_("Payment transaction (%s) failed : <%s>") % (self.id, str(e)))
                return _("Payment transaction failed (Contact Administrator)")

            valid_state = 'authorized' if self.acquirer_id.capture_manually else 'done'
            if not s2s_result or self.state != valid_state:
                return _("Payment transaction failed (%s)" % self.state_message)
            try:
                # Auto-confirm SO if necessary
                self._confirm_so()
                return True
            except Exception as e:
                _logger.warning(_("Payment transaction (%s) failed : <%s>") % (self.id, str(e)))
                return _("Payment transaction / SO Confirmation failed (Contact Administrator)")
        return _('Tx missmatch')

    def check_or_create_sale_tx(self, order, acquirer, payment_token=None, tx_type='form', add_tx_values=None, reset_draft=True):
        tx = self
        # incorrect state or unexisting tx
        if not self or self.state in ['error', 'cancel']:
            tx = False
        # unmatching
        if (self and acquirer and self.acquirer_id != acquirer) or (self and self.sale_order_id != order):
            tx = False
        # new or distinct token
        if payment_token and tx.payment_token_id and payment_token != self.payment_token_id:
            tx = False

        # still draft tx, no more info -> rewrite on tx or create a new one depending on parameter
        if tx and tx.state == 'draft':
            if reset_draft:
                tx.write(dict(
                    self.on_change_partner_id(order.partner_id.id).get('value', {}),
                    amount=order.amount_total,
                    type=tx_type)
                )
            else:
                tx = False

        if not tx:
            tx_values = {
                'acquirer_id': acquirer.id,
                'type': tx_type,
                'amount': order.amount_total,
                'currency_id': order.pricelist_id.currency_id.id,
                'partner_id': order.partner_id.id,
                'partner_country_id': order.partner_id.country_id.id,
                'reference': self.get_next_reference(order.name),
                'sale_order_id': order.id,
            }
            if add_tx_values:
                tx_values.update(add_tx_values)
            if payment_token and payment_token.sudo().partner_id == order.partner_id:
                tx_values['payment_token_id'] = payment_token.id

            tx = self.create(tx_values)

        # update quotation
        order.write({
            'payment_tx_id': tx.id,
        })

        return tx

    def render_sale_button(self, order, return_url, submit_txt=None, render_values=None):
        values = {
            'return_url': return_url,
            'partner_id': order.partner_shipping_id.id or order.partner_invoice_id.id,
            'billing_partner_id': order.partner_invoice_id.id,
        }
        if render_values:
            values.update(render_values)
        return self.acquirer_id.with_context(submit_class='btn btn-primary', submit_txt=submit_txt or _('Pay Now')).sudo().render(
            self.reference,
            order.amount_total,
            order.pricelist_id.currency_id.id,
            values=values,
        )
