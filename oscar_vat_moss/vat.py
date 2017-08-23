from __future__ import unicode_literals

from decimal import Decimal as D
import re

from gettext import gettext as _

import vat_moss.billing_address
import vat_moss.id
import vat_moss.phone_number
from vat_moss.errors import URLError
from vat_moss.errors import InvalidError, UndefinitiveError
from vat_moss.errors import WebServiceError, WebServiceUnavailableError

from oscar_vat_moss.util import u

from django.conf import settings

VERIFICATIONS_NEEDED = 2


def apply_to(submission):
    rate = lookup_vat_for_submission(submission)

    for line in submission['basket'].all_lines():
        line_tax = calculate_tax(
            line.line_price_excl_tax_incl_discounts, rate)
        unit_tax = (line_tax / line.quantity).quantize(D('0.01'))
        line.purchase_info.price.tax = unit_tax

    # Note, we change the submission in place - we don't need to
    # return anything from this function
    shipping_charge = submission['shipping_charge']
    shipping_charge.tax = calculate_tax(shipping_charge.excl_tax,
                                        rate)


def lookup_vat_for_submission(submission):
    shipping_address = submission['shipping_address']
    return lookup_vat_for_address(shipping_address)


def lookup_vat_for_user(user):
    # If we have an address that is marked as the default
    # shipping address, we'll use that. Otherwise,
    # randomly use the first address.
    tax_address = user.addresses.order_by('-is_default_for_shipping')[0]
    return lookup_vat_for_address(tax_address)


def lookup_vat_for_address(address):
    # Use getattr here so we can default to empty string for
    # non-existing fields.
    company = getattr(address, 'organisation', '')
    city = getattr(address, 'line4', '')
    country = getattr(address, 'country', '')
    postcode = getattr(address, 'postcode', '')
    phone_number = getattr(address, 'phone_number', '')
    vatin = getattr(address, 'vatin', '')

    try:
        return lookup_vat(company,
                          city,
                          country.code,
                          postcode,
                          phone_number,
                          vatin)
    except (URLError,
            WebServiceError,
            WebServiceUnavailableError):  # pragma: nocover
        message = "Temporary error in VAT assessment"
        raise VATAssessmentUnavailableException(message)


def lookup_vat(company, city, country_code, postcode, phone_number, vatin):
    verifications = 0
    address_vat_rate = None
    phone_vat_rate = None

    if vatin:
        try:
            return lookup_vat_by_vatin(country_code, vatin, company)
        except InvalidError:
            message = "Invalid VAT Identification Number (VATIN)"
            raise VATAssessmentException(message)
        except VATINCountrySameAsStoreException:
            # While we have a valid VATIN, its country is the same as
            # that of the store, so reverse charge cannot
            # apply. Assess VAT per the standard method.
            pass

    if city and country_code:
        try:
            address_vat_rate = lookup_vat_by_city(country_code,
                                                  postcode,
                                                  city)
            verifications += 1
        except UndefinitiveError:
            # We'll try the next one
            pass

    if phone_number:
        try:
            phone_vat_rate = lookup_vat_by_phone_number(phone_number,
                                                        country_code)
            verifications += 1
        except UndefinitiveError:
            pass

    if verifications < VERIFICATIONS_NEEDED:
        message = "Insufficent information for VAT assessment"
        raise VATAssessmentException(message)

    if address_vat_rate != phone_vat_rate:
        message = "Unable to work out applicable VAT rate " \
                  "based on address and phone information"
        raise VATAssessmentException(message)

    return address_vat_rate


def lookup_vat_by_vatin(country_code, vatin, company_name):
    # We already validated the VATIN through a form validator;
    # additional validation errors here shouldn't happen.
    (vatin_country,
     vatin_normalized,
     vatin_company) = vat_moss.id.validate(u(vatin))

    # Does the VATIN match the country we've been given?
    if vatin_country != country_code:
        raise CountryInvalidForVATINException(vatin,
                                              country_code)

    # We have a verified VATIN and it matches the company
    # name. Is the country the same as the store country?
    if country_code == settings.VAT_MOSS_STORE_COUNTRY_CODE:
        raise VATINCountrySameAsStoreException(vatin,
                                               country_code)

    # We have a verified, foreign VATIN. Reverse charge applies.
    return D('0.00')


def lookup_vat_by_city(country_code=None, postcode=None, city=None):
    # exception is a statutory VAT exception,
    # *not* a Python error!
    (rate,
     country,
     exception) = vat_moss.billing_address.calculate_rate(u(country_code),
                                                          u(postcode),
                                                          u(city))
    return rate


def lookup_vat_by_phone_number(phone_number=None, country_code=None):
    # exception is a statutory VAT exception,
    # *not* a Python error!
    (rate,
     country,
     exception) = vat_moss.phone_number.calculate_rate(u(phone_number),
                                                       u(country_code))
    return rate


def calculate_tax(price, rate):
    tax = price * rate
    return tax.quantize(D('0.01'))


class VATAssessmentException(Exception):

    def __init__(self, message=None):
        self.message = message

    def __str__(self):
        return self.message


class VATAssessmentUnavailableException(VATAssessmentException):
    pass


class NonMatchingVATINException(VATAssessmentException):

    def __init__(self, vatin, company_name):
        self.message = _('VATIN %s does not match company name "%s"' %
                         (vatin, company_name))
        self.vatin = vatin
        self.company_name = company_name


class CountryInvalidForVATINException(VATAssessmentException):
    def __init__(self, vatin, country):
        self.message = _('VATIN %s is not from "%s"' %
                         (vatin, country))
        self.vatin = vatin
        self.country = country


class VATINCountrySameAsStoreException(VATAssessmentException):
    def __init__(self, vatin, country):
        self.message = _('VATIN %s is from same country as store (%s)' %
                         (vatin, country))
        self.vatin = vatin
        self.country = country
