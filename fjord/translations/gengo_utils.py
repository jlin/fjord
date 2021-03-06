from functools import wraps

from django.conf import settings

import json
import requests
from gengo import Gengo, GengoError


# Cache of supported languages from Gengo. Theoretically, these don't
# change often, so the first time we request it, we cache it and then
# keep that until the next deployment.
GENGO_LANGUAGE_CACHE = None

# Cache of supported language pairs.
GENGO_LANGUAGE_PAIRS_CACHE = None

# List of unsupported source languages. Theoretically, these don't
# change often and there's no way to discover them via the Gengo API
# that I can see, so we're going to manually curate them. If this
# turns out to be problematic, we can change how we do things then.
GENGO_MACHINE_UNSUPPORTED = ['zh-cn', 'zh-tw']

# The comment we send to Gengo with the jobs to give some context for
# the job.
GENGO_COMMENT = """\
This is a response from the Mozilla Input feedback system. It was
submitted by an anonymous user in a non-English language. The feedback
is used in aggregate to determine general user sentiment about Mozilla
products and its features.

This translation job was created by an automated system, so we're
unable to respond to translator comments.

If the response is nonsensical or junk text, then write "spam".
"""


class FjordGengoError(Exception):
    """Superclass for all Gengo translation errors"""
    pass


class GengoConfigurationError(FjordGengoError):
    """Raised when the Gengo-centric keys aren't set in settings"""


class GengoUnknownLanguage(FjordGengoError):
    """Raised when the guesser can't guess the language"""


class GengoUnsupportedLanguage(FjordGengoError):
    """Raised when the guesser guesses a language Gengo doesn't support

    .. Note::

       If you buy me a beer, I'll happily tell you how I feel about
       this.

    """


class GengoAPIFailure(FjordGengoError):
    """Raised when the api kicks up an error"""


class GengoMachineTranslationFailure(FjordGengoError):
    """Raised when machine translation didn't work"""


class GengoHumanTranslationFailure(FjordGengoError):
    """Raised when human translation didn't work"""


def requires_keys(fun):
    """Throw GengoConfigurationError if keys aren't set"""
    @wraps(fun)
    def _requires_keys(self, *args, **kwargs):
        if not self.gengo_api:
            raise GengoConfigurationError()
        return fun(self, *args, **kwargs)
    return _requires_keys


class FjordGengo(object):
    def __init__(self):
        """Constructs a FjordGengo wrapper around the Gengo class

        We do this to make using the API a little easier in the
        context for Fjord as it includes the business logic around
        specific use cases we have.

        Also, having all the Gengo API stuff in one place makes it
        easier for mocking when testing.

        """
        if settings.GENGO_PUBLIC_KEY and settings.GENGO_PRIVATE_KEY:
            gengo_api = Gengo(
                public_key=settings.GENGO_PUBLIC_KEY,
                private_key=settings.GENGO_PRIVATE_KEY,
                sandbox=getattr(settings, 'GENGO_USE_SANDBOX', True)
            )
        else:
            gengo_api = None

        self.gengo_api = gengo_api

    def is_configured(self):
        """Returns whether Gengo is configured for Gengo API requests"""
        return not (self.gengo_api is None)

    @requires_keys
    def get_balance(self):
        """Returns the account balance as a float"""
        balance = self.gengo_api.getAccountBalance()
        return float(balance['response']['credits'])

    @requires_keys
    def get_languages(self, raw=False):
        """Returns the list of supported language targets

        :arg raw: True if you want the whole response, False if you
            want just the list of languages

        .. Note::

           This is cached until the next deployment.

        """
        global GENGO_LANGUAGE_CACHE
        if not GENGO_LANGUAGE_CACHE:
            resp = self.gengo_api.getServiceLanguages()
            GENGO_LANGUAGE_CACHE = (
                resp,
                tuple([item['lc'] for item in resp['response']])
            )
        if raw:
            return GENGO_LANGUAGE_CACHE[0]
        else:
            return GENGO_LANGUAGE_CACHE[1]

    @requires_keys
    def get_language_pairs(self):
        """Returns the list of supported language pairs

        .. Note::

           This is cached until the next deployment.

        """
        global GENGO_LANGUAGE_PAIRS_CACHE
        if not GENGO_LANGUAGE_PAIRS_CACHE:
            resp = self.gengo_api.getServiceLanguagePairs()
            # NB: This looks specifically at the standard tier because
            # that's what we're using. It ignores the other tiers.
            pairs = [(item['lc_src'], item['lc_tgt'])
                     for item in resp['response']
                     if item['tier'] == u'standard']
            GENGO_LANGUAGE_PAIRS_CACHE = pairs

        return GENGO_LANGUAGE_PAIRS_CACHE

    @requires_keys
    def get_job(self, job_id):
        """Returns data for a specified job

        :arg job_id: the job_id for the job we want data for

        :returns: dict of job data

        """
        resp = self.gengo_api.getTranslationJob(id=str(job_id))

        if resp['opstat'] != 'ok':
            raise GengoAPIFailure(
                'opstat: {0}, response: {1}'.format(resp['opstat'], resp))

        return resp['response']['job']

    def guess_language(self, text):
        """Guesses the language of the text

        :arg text: text to guess the language of

        :raises GengoUnknownLanguage: if the request wasn't successful
            or the guesser can't figure out which language the text is

        """
        # get_language is a "private API" thing Gengo has, so it's not
        # included in the gengo library and we have to do it manually.
        resp = requests.post(
            'https://api.gengo.com/service/detect_language',
            data=json.dumps({'text': text.encode('utf-8')}),
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            })

        resp_json = resp.json()

        if 'detected_lang_code' in resp_json:
            lang = resp_json['detected_lang_code']
            if lang == 'un':
                raise GengoUnknownLanguage('unknown language')
            return lang

        raise GengoUnknownLanguage('request failure: {0}'.format(resp.content))

    @requires_keys
    def machine_translate(self, id_, lc_src, lc_dst, text):
        """Performs a machine translation through Gengo

        This method is synchronous--it creates the request, posts it,
        waits for it to finish and then returns the translated text.

        :arg id_: instance id
        :arg lc_src: source language
        :arg lc_dst: destination language
        :arg text: the text to translate

        :returns: text

        :raises GengoUnsupportedLanguage: if the guesser guesses a
            language that Gengo doesn't support

        :raises GengoMachineTranslationFailure: if calling machine
            translation fails

        """
        if lc_src in GENGO_MACHINE_UNSUPPORTED:
            raise GengoUnsupportedLanguage(
                'unsupported language (translater; hc)): {0} -> {1}'.format(
                    lc_src, lc_dst))

        data = {
            'jobs': {
                'job_1': {
                    'custom_data': str(id_),
                    'body_src': text,
                    'lc_src': lc_src,
                    'lc_tgt': lc_dst,
                    'tier': 'machine',
                    'type': 'text',
                    'slug': 'Mozilla Input feedback response',
                }
            }
        }

        try:
            resp = self.gengo_api.postTranslationJobs(jobs=data)
        except GengoError as ge:
            # It's possible for the guesser to guess a language that's
            # in the list of supported languages, but for some reason
            # it's not actually supported which can throw a 1551
            # GengoError. In that case, we treat it as an unsupported
            # language.
            if ge.error_code == 1551:
                raise GengoUnsupportedLanguage(
                    'unsupported language (translater)): {0} -> {1}'.format(
                        lc_src, lc_dst))
            raise

        if resp['opstat'] == 'ok':
            job = resp['response']['jobs']['job_1']
            if 'body_tgt' not in job:
                raise GengoMachineTranslationFailure(
                    'no body_tgt: {0} -> {1}'.format(lc_src, lc_dst))

            return job['body_tgt']

        raise GengoAPIFailure(
            'opstat: {0}, response: {1}'.format(resp['opstat'], resp))

    @requires_keys
    def human_translate_bulk(self, jobs):
        """Performs human translation through Gengo on multiple jobs

        This method is asynchronous--it creates the request, posts it,
        and returns the order information.

        :arg jobs: a list of dicts with ``id``, ``lc_src``, ``lc_dst``
            ``text`` and (optional) ``unique_id`` keys

        Response dict includes:

        * job_count: number of jobs processed
        * order_id: the order id
        * group_id: I have no idea what this is
        * credits_used: the number of credits used
        * currency: the currency the credits are in

        """
        payload = {}
        for job in jobs:
            payload['job_{0}'.format(job['id'])] = {
                'body_src': job['text'],
                'lc_src': job['lc_src'],
                'lc_tgt': job['lc_dst'],
                'tier': 'standard',
                'type': 'text',
                'slug': 'Mozilla Input feedback response',
                'force': 1,
                'comment': GENGO_COMMENT,
                'purpose': 'Online content',
                'tone': 'informal',
                'use_preferred': 0,
                'auto_approve': 1,
                'custom_data': job.get('unique_id', job['id'])
            }

        resp = self.gengo_api.postTranslationJobs(jobs=payload)
        if resp['opstat'] != 'ok':
            raise GengoAPIFailure(
                'opstat: {0}, response: {1}'.format(resp['opstat'], resp))

        return resp['response']

    @requires_keys
    def completed_jobs_for_order(self, order_id):
        """Returns jobs for an order which are completed

        Gengo uses the status "approved" for jobs that have been
        translated and approved and are completed.

        :arg order_id: the order_id for the jobs we want to look at

        :returns: list of job data dicts; interesting fields being
            ``custom_data`` and ``body_tgt``

        """
        resp = self.gengo_api.getTranslationOrderJobs(id=str(order_id))

        if resp['opstat'] != 'ok':
            raise GengoAPIFailure(
                'opstat: {0}, response: {1}'.format(resp['opstat'], resp))

        job_ids = resp['response']['order']['jobs_approved']
        if not job_ids:
            return []

        job_ids = ','.join(job_ids)
        resp = self.gengo_api.getTranslationJobBatch(id=job_ids)

        if resp['opstat'] != 'ok':
            raise GengoAPIFailure(
                'opstat: {0}, response: {1}'.format(resp['opstat'], resp))

        return resp['response']['jobs']
