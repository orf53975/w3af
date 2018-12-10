"""
fingerprint_404.py

Copyright 2006 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
from __future__ import with_statement

import w3af.core.data.kb.config as cf
import w3af.core.controllers.output_manager as om

from w3af.core.data.dc.headers import Headers
from w3af.core.data.fuzzer.utils import rand_alnum
from w3af.core.data.url.helpers import NO_CONTENT_MSG
from w3af.core.data.db.cached_disk_dict import CachedDiskDict

from w3af.core.controllers.misc.diff import diff
from w3af.core.controllers.misc.fuzzy_string_cmp import fuzzy_equal, MAX_FUZZY_LENGTH
from w3af.core.controllers.core_helpers.not_found.response import FourOhFourResponse
from w3af.core.controllers.core_helpers.not_found.generate_404 import send_request_generate_404
from w3af.core.controllers.core_helpers.not_found.decorators import LRUCache404, PreventMultipleThreads


IS_EQUAL_RATIO = 0.90
NOT_404_RESPONSE_CODES = (200, 500, 301, 302, 303, 307, 401)
MAX_404_IN_MEMORY = 50


class Fingerprint404(object):
    """
    Read the 404 page(s) returned by the server.

    :author: Andres Riancho (andres.riancho@gmail.com)
    """

    _instance = None

    def __init__(self):
        #
        #   Set the opener, I need it to perform some tests and gain
        #   the knowledge about the server's 404 response bodies.
        #
        self._uri_opener = None
        self._worker_pool = None
        
        #
        #   Store the 404 responses in a dict which has normalized paths
        #   as keys and 404 data as values.
        #
        #   The most commonly used keys for this dict are stored in memory
        #   while the least commonly used are stored in SQLite
        #
        self._404_responses = CachedDiskDict(max_in_memory=MAX_404_IN_MEMORY,
                                             table_prefix='is_404')

    @PreventMultipleThreads
    @LRUCache404
    def is_404(self, http_response):
        """
        All of my previous versions of is_404 were very complex and tried to
        struggle with all possible cases. The truth is that in most "strange"
        cases I was failing miserably, so now I changed my 404 detection once
        again, but keeping it as simple as possible.

        Also, and because I was trying to cover ALL CASES, I was performing a
        lot of requests in order to cover them, which in most situations was
        unnecessary.

        So now I go for a much simple approach:
            1- Handle the most common case of all using only 1 HTTP request

            2- Handle rare cases with 2 HTTP requests

            3- Give the users the power to configure the 404 detection by
               setting a string that identifies the 404 response (in case we
               are missing it for some reason in cases #1 and #2)

        :param http_response: The HTTP response
        :return: True if the HTTP response is a 404
        """
        if self._is_404_basic(http_response):
            return True

        if self._is_404_complex(http_response):
            return True

        return False

    def _is_404_basic(self, http_response):
        """
        Verifies if the response is a 404 by checking the user's configuration
        and applying very basic algorithms.

        :param http_response: The HTTP response
        :return: True if the HTTP response is a 404
        """
        domain_path = http_response.get_url().get_domain_path()

        #
        # First we handle the user configured exceptions:
        #
        if domain_path in cf.cf.get('always_404'):
            return True

        if domain_path in cf.cf.get('never_404'):
            return False

        #
        # The user configured setting. "If this string is in the response,
        # then it is a 404"
        #
        if cf.cf.get('string_match_404') and cf.cf.get('string_match_404') in http_response:
            return True

        #
        # This is the most simple case, we don't even have to think about this
        #
        # If there is some custom website that always returns 404 codes, then
        # we are screwed, but this is open source, and the pentester working
        # on that site can modify these lines.
        #
        if http_response.get_code() == 404:
            return True

        #
        # This is an edge case. Let me explain...
        #
        # Doing try/except in all plugins that send HTTP requests was hard (tm)
        # so plugins don't use ExtendedUrllib directly, instead they use the
        # UrlOpenerProxy (defined in plugin.py). This proxy catches any
        # exceptions and returns a 204 response.
        #
        # In most cases that works perfectly, because it will allow the plugin
        # to keep working without caring much about the exceptions. In some
        # edge cases someone will call is_404(204_response_generated_by_w3af)
        # and that will most likely return False, because the 204 response we
        # generate doesn't look like anything w3af has in the 404 DB.
        #
        # The following iff fixes the race condition
        #
        if http_response.get_code() == 204:
            if http_response.get_msg() == NO_CONTENT_MSG:
                if http_response.get_headers() == Headers():
                    return True

        return False

    def _is_404_complex(self, http_response):
        """
        Verifies if the response is a 404 by comparing it with other responses
        which are known to be 404s, potentially sends HTTP requests to the
        server.

        :param http_response: The HTTP response
        :return: True if the HTTP response is a 404
        """
        debugging_id = rand_alnum(8)

        # 404_body stored in the DB was cleaned when creating the
        # FourOhFourResponse class.
        #
        # Clean the body received as parameter in order to have a fair
        # comparison
        query = FourOhFourResponse(http_response)

        #
        # Compare query with a known 404 from the DB (or a generated one
        # if there is none with the same path in the DB)
        #
        known_404 = self._get_404_response(http_response, query, debugging_id)

        # Trivial performance improvement that prevents running fuzzy_equal
        if query.code in NOT_404_RESPONSE_CODES and known_404.code == 404:
            msg = ('"%s" (id:%s, code:%s, len:%s, did:%s) is NOT a 404'
                   ' [known 404 with ID %s uses 404 code]')
            args = (http_response.get_url(),
                    http_response.id,
                    http_response.get_code(),
                    len(http_response.get_body()),
                    debugging_id,
                    known_404.id)
            om.out.debug(msg % args)
            return False

        # Since the fuzzy_equal function is CPU-intensive we want to
        # avoid calling it for cases where we know it won't match, for
        # example in comparing an image and an html
        if query.doc_type != known_404.doc_type:
            msg = ('"%s" (id:%s, code:%s, len:%s, did:%s) is NOT a 404'
                   ' [document type mismatch with known 404 with ID %s]')
            args = (http_response.get_url(),
                    http_response.id,
                    http_response.get_code(),
                    len(http_response.get_body()),
                    debugging_id,
                    known_404.id)
            om.out.debug(msg % args)
            return False

        # This is the simplest case. If they are 100% equal, no matter how
        # large or complex the responses are, then query is a 404
        if known_404.body == query.body:
            msg = ('"%s" (id:%s, code:%s, len:%s, did:%s) is a 404'
                   ' [string equals with 404 DB entry with ID %s]')
            args = (http_response.get_url(),
                    http_response.id,
                    http_response.get_code(),
                    len(http_response.get_body()),
                    debugging_id,
                    known_404.id)
            om.out.debug(msg % args)
            return True

        is_fuzzy_equal = fuzzy_equal(known_404.body, query.body, IS_EQUAL_RATIO)

        if not is_fuzzy_equal:
            msg = ('"%s" (id:%s, code:%s, len:%s, did:%s) is NOT a 404'
                   ' [similarity_ratio < %s with known 404 with ID %s]')
            args = (http_response.get_url(),
                    http_response.id,
                    http_response.get_code(),
                    len(http_response.get_body()),
                    debugging_id,
                    IS_EQUAL_RATIO,
                    known_404.id)
            om.out.debug(msg % args)
            return False

        if len(query.body) < MAX_FUZZY_LENGTH:
            # The response bodies are fuzzy-equal, and the length is less than
            # MAX_FUZZY_LENGTH. This is good, it means that they are equal and
            # long headers / footers in HTTP response bodies are not
            # interfering with fuzzy-equals.
            #
            # Some sites have really large headers and footers which they
            # include for all pages, including 404s. When that happens one page
            # might look like:
            #
            #   {header-4000bytes}
            #   Hello world
            #   {footer-4000bytes}
            #
            # The header might contain large CSS and the footer might include
            # JQuery or some other large JS. Then, the 404 might look like:
            #
            #   {header-4000bytes}
            #   Not found
            #   {footer-4000bytes}
            #
            # A user with a browser might only see the text, and clearly
            # identify one as a valid page and another as a 404, but the
            # fuzzy_equal() function will return True, indicating that they
            # are equal because 99% of the bytes are the same.
            msg = ('"%s" (id:%s, code:%s, len:%s, did:%s) is a 404'
                   ' [similarity_ratio > %s with 404 DB entry with ID %s]')
            args = (http_response.get_url(),
                    http_response.id,
                    http_response.get_code(),
                    len(http_response.get_body()),
                    debugging_id,
                    IS_EQUAL_RATIO,
                    known_404.id)
            om.out.debug(msg % args)
            return True

        else:
            # See the large comment above on why we need to check for
            # MAX_FUZZY_LENGTH.
            #
            # The way to handle this case is to send an extra HTTP
            # request that will act as a tie-breaker.
            return self._handle_large_http_responses(http_response,
                                                     query,
                                                     known_404,
                                                     debugging_id)

    def _handle_large_http_responses(self, http_response, query, known_404, debugging_id):
        """
        When HTTP response bodies are large the fuzzy_equal() will generate
        404 false positives. This is explained in a comment above,
        (search for "{header-4000bytes}").

        This method will handle that case by using three HTTP responses instead
        of two (which is the most common case). The three HTTP responses used
        by this method are:

            * known_404: The forced 404 generated by this class
            * query:  The HTTP response we want to know if it is a 404
            * Another forced 404 generated by this method

        The method will diff the two 404 responses, and one 404 response with
        the query response, then compare using fuzzy_equal() to determine if the
        query is a 404.

        :return: True if the query response is a 404!
        """
        # Make the algorithm easier to read
        known_404_1 = known_404

        if known_404_1.diff is not None:
            # At some point during the execution of this scan we already sent
            # an HTTP request to use in this process and calculated the diff
            #
            # In order to prevent more HTTP requests from being sent to the
            # server, and also to reduce CPU usage, we saved the diff as an
            # attribute.
            pass
        else:
            # Need to send the second request and calculate the diff, there is
            # no previous knowledge that we can use
            #
            # Send exclude=[known_404_1.url] to prevent the function from sending
            # an HTTP request to the same forced 404 URL
            known_404_2 = send_request_generate_404(self._uri_opener,
                                                    http_response,
                                                    debugging_id,
                                                    exclude=[known_404_1.url])

            known_404_1.diff, _ = diff(known_404_1.body, known_404_2.body)
            self._404_responses[query.normalized_path] = known_404_1

        if known_404_1.diff == '':
            # The two known 404 we generated are equal, and we only get here
            # if the query is not equal to known_404_1, this means that the
            # application is using the same 404 response body for all responses
            # in this path, but did not use that one for the `query` response.
            msg = ('"%s" (id:%s, code:%s, len:%s, did:%s) is NOT a 404'
                   ' [the two known 404 responses are equal (id:%s)]')
            args = (http_response.get_url(),
                    http_response.id,
                    http_response.get_code(),
                    len(http_response.get_body()),
                    debugging_id,
                    known_404.id)
            om.out.debug(msg % args)
            return False

        diff_x = known_404_1.diff
        _, diff_y = diff(known_404_1.body, query.body)

        is_fuzzy_equal = fuzzy_equal(diff_x, diff_y, IS_EQUAL_RATIO)

        if not is_fuzzy_equal:
            msg = ('"%s" (id:%s, code:%s, len:%s, did:%s) is NOT a 404'
                   ' [similarity_ratio < %s with diff of 404 with ID %s]')
            args = (http_response.get_url(),
                    http_response.id,
                    http_response.get_code(),
                    len(http_response.get_body()),
                    debugging_id,
                    IS_EQUAL_RATIO,
                    known_404.id)
            om.out.debug(msg % args)
            return False

        msg = ('"%s" (id:%s, code:%s, len:%s, did:%s) is a 404'
               ' [similarity_ratio > %s with diff of 404 with ID %s]')
        args = (http_response.get_url(),
                http_response.id,
                http_response.get_code(),
                len(http_response.get_body()),
                debugging_id,
                IS_EQUAL_RATIO,
                known_404.id)
        om.out.debug(msg % args)
        return True

    def set_url_opener(self, urlopener):
        self._uri_opener = urlopener

    def set_worker_pool(self, worker_pool):
        self._worker_pool = worker_pool

    def _get_404_response(self, http_response, query, debugging_id):
        """
        :return: A FourOhFourResponse instance.
                    * First try to get the response from the 404 DB

                    * If the data is not there then send an HTTP request
                    with a randomly generated path or name to force a 404,
                    save the data to the DB and then return it.
        """
        known_404 = self._404_responses.get(query.normalized_path, None)
        if known_404 is not None:
            return known_404

        known_404 = send_request_generate_404(self._uri_opener,
                                              http_response,
                                              debugging_id)

        self._404_responses[query.normalized_path] = known_404
        return known_404


def fingerprint_404_singleton(cleanup=False):
    if Fingerprint404._instance is None or cleanup:
        Fingerprint404._instance = Fingerprint404()

    return Fingerprint404._instance


#
# Helper function
#
def is_404(http_response):
    # Get an instance of the 404 database
    fp_404_db = fingerprint_404_singleton()
    return fp_404_db.is_404(http_response)



