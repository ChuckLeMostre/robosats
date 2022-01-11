from datetime import timedelta
from django.utils import timezone
from .lightning.node import LNNode

from .models import Order, LNPayment, MarketTick, User
from decouple import config
from .utils import get_exchange_rate

import math

FEE = float(config('FEE'))
BOND_SIZE = float(config('BOND_SIZE'))
MARKET_PRICE_API = config('MARKET_PRICE_API')
ESCROW_USERNAME = config('ESCROW_USERNAME')
PENALTY_TIMEOUT = int(config('PENALTY_TIMEOUT'))

MIN_TRADE = int(config('MIN_TRADE'))
MAX_TRADE = int(config('MAX_TRADE'))

EXP_MAKER_BOND_INVOICE = int(config('EXP_MAKER_BOND_INVOICE'))
EXP_TAKER_BOND_INVOICE = int(config('EXP_TAKER_BOND_INVOICE'))
EXP_TRADE_ESCR_INVOICE = int(config('EXP_TRADE_ESCR_INVOICE'))

BOND_EXPIRY = int(config('BOND_EXPIRY'))
ESCROW_EXPIRY = int(config('ESCROW_EXPIRY'))


class Logics():

    def validate_already_maker_or_taker(user):
        '''Checks if the user is already partipant of an order'''
        queryset = Order.objects.filter(maker=user)
        if queryset.exists():
            return False, {'bad_request':'You are already maker of an order'}
        queryset = Order.objects.filter(taker=user)
        if queryset.exists():
            return False, {'bad_request':'You are already taker of an order'}
        return True, None

    def validate_order_size(order):
        '''Checks if order is withing limits at t0'''
        if order.t0_satoshis > MAX_TRADE:
            return False, {'bad_request': 'Your order is too big. It is worth '+'{:,}'.format(order.t0_satoshis)+' Sats now. But limit is '+'{:,}'.format(MAX_TRADE)+ ' Sats'}
        if order.t0_satoshis < MIN_TRADE:
            return False, {'bad_request': 'Your order is too small. It is worth '+'{:,}'.format(order.t0_satoshis)+' Sats now. But limit is '+'{:,}'.format(MIN_TRADE)+ ' Sats'}
        return True, None

    @classmethod    
    def take(cls, order, user):
        is_penalized, time_out = cls.is_penalized(user)
        if is_penalized:
            return False, {'bad_request',f'You need to wait {time_out} seconds to take an order'}
        else:
            order.taker = user
            order.status = Order.Status.TAK
            order.save()
            return True, None

    def is_buyer(order, user):
        is_maker = order.maker == user
        is_taker = order.taker == user
        return (is_maker and order.type == Order.Types.BUY) or (is_taker and order.type == Order.Types.SELL)

    def is_seller(order, user):
        is_maker = order.maker == user
        is_taker = order.taker == user
        return (is_maker and order.type == Order.Types.SELL) or (is_taker and order.type == Order.Types.BUY)
    
    def satoshis_now(order):
        ''' checks trade amount in sats '''
        if order.is_explicit:
            satoshis_now = order.satoshis
        else:
            exchange_rate = get_exchange_rate(Order.currency_dict[str(order.currency)])
            premium_rate = exchange_rate * (1+float(order.premium)/100)
            satoshis_now = (float(order.amount) / premium_rate) * 100*1000*1000

        return int(satoshis_now)

    def price_and_premium_now(order):
        ''' computes order premium live '''
        exchange_rate = get_exchange_rate(Order.currency_dict[str(order.currency)])
        if not order.is_explicit:
            premium = order.premium
            price = exchange_rate * (1+float(premium)/100)
        else:
            exchange_rate = get_exchange_rate(Order.currency_dict[str(order.currency)])
            order_rate = float(order.amount) / (float(order.satoshis) / 100000000)
            premium = order_rate / exchange_rate - 1
            premium = int(premium*100)  # 2 decimals left
            price = order_rate

        significant_digits = 6
        price = round(price, significant_digits - int(math.floor(math.log10(abs(price)))) - 1)
        
        return price, premium

    def order_expires(order):
        order.status = Order.Status.EXP
        order.maker = None
        order.taker = None
        order.save()

    @classmethod
    def buyer_invoice_amount(cls, order, user):
        ''' Computes buyer invoice amount. Uses order.last_satoshis, 
        that is the final trade amount set at Taker Bond time'''

        if cls.is_buyer(order, user):
            invoice_amount = int(order.last_satoshis * (1-FEE)) # Trading FEE is charged here.

        return True, {'invoice_amount': invoice_amount}

    @classmethod
    def update_invoice(cls, order, user, invoice):
        
        # only the buyer can post a buyer invoice
        if not cls.is_buyer(order, user):
            return False, {'bad_request':'Only the buyer of this order can provide a buyer invoice.'}
        if not order.taker_bond:
            return False, {'bad_request':'Wait for your order to be taken.'}
        if not (order.taker_bond.status == order.maker_bond.status == LNPayment.Status.LOCKED):
            return False, {'bad_request':'You cannot a invoice while bonds are not posted.'}

        num_satoshis = cls.buyer_invoice_amount(order, user)[1]['invoice_amount']
        buyer_invoice = LNNode.validate_ln_invoice(invoice, num_satoshis)
 
        if not buyer_invoice['valid']:
            return False, buyer_invoice['context']

        order.buyer_invoice, _ = LNPayment.objects.update_or_create(
            concept = LNPayment.Concepts.PAYBUYER, 
            type = LNPayment.Types.NORM, 
            sender = User.objects.get(username=ESCROW_USERNAME),
            receiver= user, 
            # if there is a LNPayment matching these above, it updates that one with defaults below.
            defaults={
                'invoice' : invoice,
                'status' : LNPayment.Status.VALIDI,
                'num_satoshis' : num_satoshis,
                'description' :  buyer_invoice['description'],
                'payment_hash' : buyer_invoice['payment_hash'],
                'created_at' : buyer_invoice['created_at'],
                'expires_at' : buyer_invoice['expires_at']}
            )

        # If the order status is 'Waiting for escrow'. Move forward to 'chat'
        if order.status == Order.Status.WFE: order.status = Order.Status.CHA

        # If the order status is 'Waiting for both'. Move forward to 'waiting for escrow'
        if order.status == Order.Status.WF2:
            if order.trade_escrow:
                if order.trade_escrow.status == LNPayment.Status.LOCKED:
                    order.status = Order.Status.CHA
            else:
                order.status = Order.Status.WFE

        order.save()
        return True, None

    def add_profile_rating(profile, rating):
        ''' adds a new rating to a user profile'''

        profile.total_ratings = profile.total_ratings + 1
        latest_ratings = profile.latest_ratings
        if len(latest_ratings) <= 1:
            profile.latest_ratings = [rating]
            profile.avg_rating = rating

        else:
            latest_ratings = list(latest_ratings).append(rating)
            profile.latest_ratings = latest_ratings
            profile.avg_rating = sum(latest_ratings) / len(latest_ratings)

        profile.save()

    @classmethod
    def rate_counterparty(cls, order, user, rating):

        # If the trade is finished
        if order.status > Order.Status.PAY:
            # if maker, rates taker
            if order.maker == user:
                cls.add_profile_rating(order.taker.profile, rating)
            # if taker, rates maker
            if order.taker == user:
                cls.add_profile_rating(order.maker.profile, rating)
        else:
            return False, {'bad_request':'You cannot rate your counterparty yet.'}

        return True, None

    def is_penalized(user):
        ''' Checks if a user that is not participant of orders
        has a limit on taking or making a order'''
        
        if user.profile.penalty_expiration:
            if user.profile.penalty_expiration > timezone.now():
                time_out = (user.profile.penalty_expiration - timezone.now()).seconds
                return True, time_out

        return False, None


    @classmethod
    def cancel_order(cls, order, user, state=None):

        # 1) When maker cancels before bond
        '''The order never shows up on the book and order 
        status becomes "cancelled". That's it.'''
        if order.status == Order.Status.WFB and order.maker == user:
            order.maker = None
            order.status = Order.Status.UCA
            order.save()
            return True, {}

        # 2) When maker cancels after bond
            '''The order dissapears from book and goes to cancelled. 
            Maker is charged the bond to prevent DDOS 
            on the LN node and order book. TODO Only charge a small part 
            of the bond (requires maker submitting an invoice)'''
        elif order.status == Order.Status.PUB and order.maker == user:
            #Settle the maker bond (Maker loses the bond for a public order)
            if cls.settle_maker_bond(order):
                order.maker_bond.status = LNPayment.Status.SETLED
                order.maker_bond.save()

                order.maker = None
                order.status = Order.Status.UCA
                order.save()
                return True, {}

        # 3) When taker cancels before bond
            ''' The order goes back to the book as public.
            LNPayment "order.taker_bond" is deleted() '''
        elif order.status == Order.Status.TAK and order.taker == user:
            # adds a timeout penalty
            user.profile.penalty_expiration = timezone.now() + timedelta(seconds=PENALTY_TIMEOUT)
            user.profile.save()

            order.taker = None
            order.status = Order.Status.PUB
            order.save()

            return True, {}

        # 4) When taker or maker cancel after bond (before escrow)
            '''The order goes into cancelled status if maker cancels.
            The order goes into the public book if taker cancels.
            In both cases there is a small fee.'''

        # 4.a) When maker cancel after bond (before escrow)
            '''The order into cancelled status if maker cancels.'''
        elif order.status > Order.Status.PUB and order.status < Order.Status.CHA and order.maker == user:
            #Settle the maker bond (Maker loses the bond for canceling an ongoing trade)
            valid = cls.settle_maker_bond(order)
            if valid:
                order.maker = None
                order.status = Order.Status.UCA
                order.save()
                return True, {}

        # 4.b) When taker cancel after bond (before escrow)
            '''The order into cancelled status if maker cancels.'''
        elif order.status > Order.Status.TAK and order.status < Order.Status.CHA and order.taker == user:
            # Settle the maker bond (Maker loses the bond for canceling an ongoing trade)
            valid = cls.settle_taker_bond(order)
            if valid:
                order.taker = None
                order.status = Order.Status.PUB
                # order.taker_bond = None # TODO fix this, it overrides the information about the settled taker bond. Might make admin tasks hard.
                order.save()
                return True, {}

        # 5) When trade collateral has been posted (after escrow)
            '''Always goes to cancelled status. Collaboration  is needed.
            When a user asks for cancel, 'order.is_pending_cancel' goes True.
            When the second user asks for cancel. Order is totally cancelled.
            Has a small cost for both parties to prevent node DDOS.'''
        
        else:
            return False, {'bad_request':'You cannot cancel this order'}

    @classmethod
    def gen_maker_hold_invoice(cls, order, user):

        # Do not gen and cancel if order is more than 5 minutes old
        if order.expires_at < timezone.now():
            cls.order_expires(order)
            return False, {'bad_request':'Invoice expired. You did not confirm publishing the order in time. Make a new order.'}

        # Return the previous invoice if there was one and is still unpaid
        if order.maker_bond:
            cls.check_maker_bond_locked(order)
            if order.maker_bond.status == LNPayment.Status.INVGEN:
                return True, {'bond_invoice':order.maker_bond.invoice,'bond_satoshis':order.maker_bond.num_satoshis}
            else:
                return False, None

        order.last_satoshis = cls.satoshis_now(order)
        bond_satoshis = int(order.last_satoshis * BOND_SIZE)

        description = f"RoboSats - Publishing '{str(order)}' - This is a maker bond, it will freeze in your wallet. It automatically returns. It will be charged if you cheat or cancel."

        # Gen hold Invoice
        hold_payment = LNNode.gen_hold_invoice(bond_satoshis, description, BOND_EXPIRY*3600)
        
        order.maker_bond = LNPayment.objects.create(
            concept = LNPayment.Concepts.MAKEBOND, 
            type = LNPayment.Types.HOLD, 
            sender = user,
            receiver = User.objects.get(username=ESCROW_USERNAME),
            invoice = hold_payment['invoice'],
            preimage = hold_payment['preimage'],
            status = LNPayment.Status.INVGEN,
            num_satoshis = bond_satoshis,
            description =  description,
            payment_hash = hold_payment['payment_hash'],
            created_at = hold_payment['created_at'],
            expires_at = hold_payment['expires_at'])

        order.save()
        return True, {'bond_invoice':hold_payment['invoice'], 'bond_satoshis':bond_satoshis}

    @classmethod
    def check_until_maker_bond_locked(cls, order):
        expiration = order.maker_bond.created_at + timedelta(seconds=EXP_MAKER_BOND_INVOICE)
        is_locked = LNNode.check_until_invoice_locked(order.payment_hash, expiration)
        
        if is_locked:
            order.maker_bond.status = LNPayment.Status.LOCKED
            order.maker_bond.save()
            order.status = Order.Status.PUB

        order.save()
        return is_locked

    @classmethod
    def check_maker_bond_locked(cls, order):
        if LNNode.validate_hold_invoice_locked(order.maker_bond.payment_hash):
            order.maker_bond.status = LNPayment.Status.LOCKED
            order.maker_bond.save()
            order.status = Order.Status.PUB
            order.save()
            return True
        return False

    @classmethod
    def gen_taker_hold_invoice(cls, order, user):

        # Do not gen and cancel if a taker invoice is there and older than X minutes and unpaid still
        if order.taker_bond:
            # Check if status is INVGEN and still not expired
            if order.taker_bond.status == LNPayment.Status.INVGEN:
                if order.taker_bond.created_at > (timezone.now()+timedelta(minutes=EXP_TAKER_BOND_INVOICE)):
                    cls.cancel_order(order, user, 3) # State 3, cancel order before taker bond
                    return False, {'bad_request':'Invoice expired. You did not confirm taking the order in time.'}
                # Return the previous invoice there was with INVGEN status
                else:
                    return True, {'bond_invoice':order.taker_bond.invoice,'bond_satoshis':order.taker_bond.num_satoshis}
            # Invoice exists, but was already locked or settled
            else:
                return False, None

        order.last_satoshis = cls.satoshis_now(order)  # LOCKS THE AMOUNT OF SATOSHIS FOR THE TRADE
        bond_satoshis = int(order.last_satoshis * BOND_SIZE)
        description = f"RoboSats - Taking '{str(order)}' - This is a taker bond, it will freeze in your wallet. It automatically returns. It will be charged if you cheat or cancel."

        # Gen hold Invoice
        hold_payment = LNNode.gen_hold_invoice(bond_satoshis, description, BOND_EXPIRY*3600)
        
        order.taker_bond = LNPayment.objects.create(
            concept = LNPayment.Concepts.TAKEBOND, 
            type = LNPayment.Types.HOLD, 
            sender = user,
            receiver = User.objects.get(username=ESCROW_USERNAME),
            invoice = hold_payment['invoice'],
            preimage = hold_payment['preimage'],
            status = LNPayment.Status.INVGEN,
            num_satoshis = bond_satoshis,
            description =  description,
            payment_hash = hold_payment['payment_hash'],
            created_at = hold_payment['created_at'],
            expires_at = hold_payment['expires_at'])

        # Extend expiry time to allow for escrow deposit
        ## Not here, on func for confirming taker collar. order.expires_at = timezone.now() + timedelta(minutes=EXP_TRADE_ESCR_INVOICE)
        
        order.save()
        return True, {'bond_invoice': hold_payment['invoice'], 'bond_satoshis': bond_satoshis}

    @classmethod
    def gen_escrow_hold_invoice(cls, order, user):
        # Do not generate and cancel if an invoice is there and older than X minutes and unpaid still
        if order.trade_escrow:
            # Check if status is INVGEN and still not expired
            if order.trade_escrow.status == LNPayment.Status.INVGEN:
                if order.trade_escrow.created_at > (timezone.now()+timedelta(minutes=EXP_TRADE_ESCR_INVOICE)): # Expired
                    cls.cancel_order(order, user, 4) # State 4, cancel order before trade escrow locked
                    return False, {'bad_request':'Invoice expired. You did not lock the trade escrow in time.'}
                # Return the previous invoice there was with INVGEN status
                else:
                    return True, {'escrow_invoice': order.trade_escrow.invoice, 'escrow_satoshis':order.trade_escrow.num_satoshis}
            # Invoice exists, but was already locked or settled
            else:
                return False, None # Does not return any context of a healthy locked escrow

        escrow_satoshis = order.last_satoshis # Trade sats amount was fixed at the time of taker bond generation (order.last_satoshis)
        description = f"RoboSats - Escrow amount for '{str(order)}' - The escrow will be released to the buyer once you confirm you received the fiat. It will automatically return if buyer does not pay."

        # Gen hold Invoice
        hold_payment = LNNode.gen_hold_invoice(escrow_satoshis, description, ESCROW_EXPIRY*3600)
        
        order.trade_escrow = LNPayment.objects.create(
            concept = LNPayment.Concepts.TRESCROW, 
            type = LNPayment.Types.HOLD, 
            sender = user,
            receiver = User.objects.get(username=ESCROW_USERNAME),
            invoice = hold_payment['invoice'],
            preimage = hold_payment['preimage'],
            status = LNPayment.Status.INVGEN,
            num_satoshis = escrow_satoshis,
            description =  description,
            payment_hash = hold_payment['payment_hash'],
            created_at = hold_payment['created_at'],
            expires_at = hold_payment['expires_at'])

        order.save()
        return True, {'escrow_invoice':hold_payment['invoice'],'escrow_satoshis': escrow_satoshis}
    
    def settle_escrow(order):
        ''' Settles the trade escrow hold invoice'''
        # TODO ERROR HANDLING
        valid = LNNode.settle_hold_invoice(order.trade_escrow.preimage)
        if valid:
            order.trade_escrow.status = LNPayment.Status.SETLED
            order.save()

        return valid

    def settle_maker_bond(order):
        ''' Settles the maker bond hold invoice'''
        # TODO ERROR HANDLING
        if LNNode.settle_hold_invoice(order.maker_bond.preimage):
            order.maker_bond.status = LNPayment.Status.SETLED
            order.save()
            return True

    def settle_taker_bond(order):
        ''' Settles the taker bond hold invoice'''
        # TODO ERROR HANDLING
        valid = LNNode.settle_hold_invoice(order.taker_bond.preimage)
        if valid:
            order.taker_bond.status = LNPayment.Status.SETLED
            order.save()

        return valid

    def pay_buyer_invoice(order):
        ''' Pay buyer invoice'''
        # TODO ERROR HANDLING

        valid = LNNode.pay_invoice(order.buyer_invoice.invoice)
        return valid

    @classmethod
    def confirm_fiat(cls, order, user):
        ''' If Order is in the CHAT states:
        If user is buyer: mark FIAT SENT and settle escrow!
        If User is the seller and FIAT is SENT: Pay buyer invoice!'''

        if order.status == Order.Status.CHA or order.status == Order.Status.FSE: # TODO Alternatively, if all collateral is locked? test out
            
            # If buyer, settle escrow and mark fiat sent
            if cls.is_buyer(order, user):
                if cls.settle_escrow(order): ##### !!! KEY LINE - SETTLES THE TRADE ESCROW !!!
                    order.trade_escrow.status = LNPayment.Status.SETLED
                    order.status = Order.Status.FSE
                    order.is_fiat_sent = True

            # If seller and fiat sent, pay buyer invoice
            elif cls.is_seller(order, user):
                if not order.is_fiat_sent:
                    return False, {'bad_request':'You cannot confirm to have received the fiat before it is confirmed to be sent by the buyer.'}
                
                # Make sure the trade escrow is at least as big as the buyer invoice 
                if order.trade_escrow.num_satoshis > order.buyer_invoice.num_satoshis:
                    return False, {'bad_request':'Woah, something broke badly. Report in the public channels, or open a Github Issue.'}
                
                # Double check the escrow is settled.
                if LNNode.double_check_htlc_is_settled(order.trade_escrow.payment_hash): 
                    if cls.pay_buyer_invoice(order): ##### !!! KEY LINE - PAYS THE BUYER INVOICE !!!
                        order.status = Order.Status.PAY
                        order.buyer_invoice.status = LNPayment.Status.PAYING
        else:
            return False, {'bad_request':'You cannot confirm the fiat payment at this stage'}

        order.save()
        return True, None