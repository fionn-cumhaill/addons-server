# -*- coding: utf-8 -*-
import json
import time
import urlparse
from datetime import datetime, timedelta

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.files import temp
from django.core.files.base import File as DjangoFile
from django.utils.datastructures import SortedDict
from django.test.utils import override_settings

from lxml.html import fromstring, HTMLParser
import mock
from mock import Mock, patch
from pyquery import PyQuery as pq

from olympia import amo, reviews
from olympia.amo.tests import TestCase
from olympia.abuse.models import AbuseReport
from olympia.access.models import Group, GroupUser
from olympia.addons.models import Addon, AddonDependency, AddonUser
from olympia.amo.tests import check_links, formset, initial
from olympia.amo.urlresolvers import reverse
from olympia.constants.base import REVIEW_LIMITED_DELAY_HOURS
from olympia.devhub.models import ActivityLog
from olympia.editors.models import EditorSubscription, ReviewerScore
from olympia.files.models import File, FileValidation
from olympia.reviews.models import Review, ReviewFlag
from olympia.users.models import UserProfile
from olympia.versions.models import ApplicationsVersions, AppVersion, Version
from olympia.zadmin.models import get_config, set_config

from .test_models import create_addon_file


class EditorTest(TestCase):
    fixtures = ['base/users', 'base/approvals', 'editors/pending-queue']

    def login_as_admin(self):
        assert self.client.login(email='admin@mozilla.com')

    def login_as_editor(self):
        assert self.client.login(email='editor@mozilla.com')

    def login_as_senior_editor(self):
        assert self.client.login(email='senioreditor@mozilla.com')

    def make_review(self, username='a'):
        u = UserProfile.objects.create(username=username)
        a = Addon.objects.create(name='yermom', type=amo.ADDON_EXTENSION)
        return Review.objects.create(user=u, addon=a)

    def _test_breadcrumbs(self, expected=None):
        if expected is None:
            expected = []
        r = self.client.get(self.url)
        expected.insert(0, ('Editor Tools', reverse('editors.home')))
        check_links(expected, pq(r.content)('#breadcrumbs li'), verify=False)


class TestEventLog(EditorTest):

    def setUp(self):
        super(TestEventLog, self).setUp()
        self.login_as_editor()
        self.url = reverse('editors.eventlog')
        amo.set_user(UserProfile.objects.get(username='editor'))

    def test_log(self):
        r = self.client.get(self.url)
        assert r.status_code == 200

    def test_start_filter(self):
        r = self.client.get(self.url, dict(start='2011-01-01'))
        assert r.status_code == 200

    def test_enddate_filter(self):
        """
        Make sure that if our end date is 1/1/2011, that we include items from
        1/1/2011.  To not do as such would be dishonorable.
        """
        review = self.make_review(username='b')
        amo.log(amo.LOG.APPROVE_REVIEW, review, review.addon,
                created=datetime(2011, 1, 1))

        r = self.client.get(self.url, dict(end='2011-01-01'))
        assert r.status_code == 200
        assert pq(r.content)('tbody td').eq(0).text() == (
            'Jan 1, 2011, 12:00:00 AM')

    def test_action_filter(self):
        """
        Based on setup we should see only two items if we filter for deleted
        reviews.
        """
        review = self.make_review()
        for i in xrange(2):
            amo.log(amo.LOG.APPROVE_REVIEW, review, review.addon)
            amo.log(amo.LOG.DELETE_REVIEW, review.id, review.addon)
        r = self.client.get(self.url, dict(filter='deleted'))
        assert pq(r.content)('tbody tr').length == 2

    def test_no_results(self):
        r = self.client.get(self.url, dict(end='2004-01-01'))
        assert '"no-results"' in r.content, 'Expected no results to be found.'

    def test_breadcrumbs(self):
        self._test_breadcrumbs([('Moderated Review Log', None)])


class TestEventLogDetail(TestEventLog):

    def test_me(self):
        review = self.make_review()
        amo.log(amo.LOG.APPROVE_REVIEW, review, review.addon)
        id = ActivityLog.objects.editor_events()[0].id
        r = self.client.get(reverse('editors.eventlog.detail', args=[id]))
        assert r.status_code == 200


class TestBetaSignedLog(EditorTest):

    def setUp(self):
        super(TestBetaSignedLog, self).setUp()
        self.login_as_editor()
        self.url = reverse('editors.beta_signed_log')
        amo.set_user(UserProfile.objects.get(username='editor'))
        addon = amo.tests.addon_factory()
        version = addon.versions.get()
        self.file1 = version.files.get()
        self.file2 = amo.tests.file_factory(version=version)
        self.file1_url = reverse('files.list', args=[self.file1.pk])
        self.file2_url = reverse('files.list', args=[self.file2.pk])

        self.log1 = amo.log(amo.LOG.BETA_SIGNED_VALIDATION_PASSED, self.file1)
        self.log2 = amo.log(amo.LOG.BETA_SIGNED_VALIDATION_FAILED, self.file2)

    def test_log(self):
        response = self.client.get(self.url)
        assert response.status_code == 200

    def test_action_no_filter(self):
        response = self.client.get(self.url)
        results = pq(response.content)('tbody tr')
        assert results.length == 2
        assert self.file1_url in unicode(results)
        assert self.file2_url in unicode(results)

    def test_action_filter_validation_passed(self):
        response = self.client.get(
            self.url, {'filter': amo.LOG.BETA_SIGNED_VALIDATION_PASSED.id})
        results = pq(response.content)('tbody tr')
        assert results.length == 1
        assert self.file1_url in unicode(results)
        assert self.file2_url not in unicode(results)

    def test_action_filter_validation_failed(self):
        response = self.client.get(
            self.url, {'filter': amo.LOG.BETA_SIGNED_VALIDATION_FAILED.id})
        results = pq(response.content)('tbody tr')
        assert results.length == 1
        assert self.file1_url not in unicode(results)
        assert self.file2_url in unicode(results)

    def test_no_results(self):
        ActivityLog.objects.all().delete()
        response = self.client.get(self.url)
        assert '"no-results"' in response.content

    def test_breadcrumbs(self):
        self._test_breadcrumbs([('Signed Beta Files Log', None)])


class TestReviewLog(EditorTest):
    fixtures = EditorTest.fixtures + ['base/addon_3615']

    def setUp(self):
        super(TestReviewLog, self).setUp()
        self.login_as_editor()
        self.url = reverse('editors.reviewlog')

    def get_user(self):
        return UserProfile.objects.all()[0]

    def make_approvals(self):
        for addon in Addon.objects.all():
            amo.log(amo.LOG.REJECT_VERSION, addon, addon.current_version,
                    user=self.get_user(), details={'comments': 'youwin'})

    def make_an_approval(self, action, comment='youwin', username=None,
                         addon=None):
        if username:
            user = UserProfile.objects.get(username=username)
        else:
            user = self.get_user()
        if not addon:
            addon = Addon.objects.all()[0]
        amo.log(action, addon, addon.current_version, user=user,
                details={'comments': comment})

    def test_basic(self):
        self.make_approvals()
        r = self.client.get(self.url)
        assert r.status_code == 200
        doc = pq(r.content)
        assert doc('#log-filter button'), 'No filters.'
        # Should have 2 showing.
        rows = doc('tbody tr')
        assert rows.filter(':not(.hide)').length == 2
        assert rows.filter('.hide').eq(0).text() == 'youwin'
        # Should have none showing if the addons are unlisted.
        for addon in Addon.objects.all():
            self.make_addon_unlisted(addon)
        r = self.client.get(self.url)
        assert r.status_code == 200
        doc = pq(r.content)
        assert not doc('tbody tr :not(.hide)')
        # But they should have 2 showing for a senior editor.
        self.login_as_senior_editor()
        r = self.client.get(self.url)
        assert r.status_code == 200
        doc = pq(r.content)
        rows = doc('tbody tr')
        assert rows.filter(':not(.hide)').length == 2
        assert rows.filter('.hide').eq(0).text() == 'youwin'

    def test_xss(self):
        a = Addon.objects.all()[0]
        a.name = '<script>alert("xss")</script>'
        a.save()
        amo.log(amo.LOG.REJECT_VERSION, a, a.current_version,
                user=self.get_user(), details={'comments': 'xss!'})

        r = self.client.get(self.url)
        assert r.status_code == 200
        inner_html = pq(r.content)('#log-listing tbody td').eq(1).html()

        assert '&lt;script&gt;' in inner_html
        assert '<script>' not in inner_html

    def test_end_filter(self):
        """
        Let's use today as an end-day filter and make sure we see stuff if we
        filter.
        """
        self.make_approvals()
        # Make sure we show the stuff we just made.
        date = time.strftime('%Y-%m-%d')
        r = self.client.get(self.url, dict(end=date))
        assert r.status_code == 200
        doc = pq(r.content)('#log-listing tbody')
        assert doc('tr:not(.hide)').length == 2
        assert doc('tr.hide').eq(0).text() == 'youwin'

    def test_end_filter_wrong(self):
        """
        Let's use today as an end-day filter and make sure we see stuff if we
        filter.
        """
        self.make_approvals()
        r = self.client.get(self.url, dict(end='wrong!'))
        # If this is broken, we'll get a traceback.
        assert r.status_code == 200
        assert pq(r.content)('#log-listing tr:not(.hide)').length == 3

    def test_search_comment_exists(self):
        """Search by comment."""
        self.make_an_approval(amo.LOG.REQUEST_SUPER_REVIEW, comment='hello')
        r = self.client.get(self.url, dict(search='hello'))
        assert r.status_code == 200
        assert pq(r.content)(
            '#log-listing tbody tr.hide').eq(0).text() == 'hello'

    def test_search_comment_case_exists(self):
        """Search by comment, with case."""
        self.make_an_approval(amo.LOG.REQUEST_SUPER_REVIEW, comment='hello')
        r = self.client.get(self.url, dict(search='HeLlO'))
        assert r.status_code == 200
        assert pq(r.content)(
            '#log-listing tbody tr.hide').eq(0).text() == 'hello'

    def test_search_comment_doesnt_exist(self):
        """Search by comment, with no results."""
        self.make_an_approval(amo.LOG.REQUEST_SUPER_REVIEW, comment='hello')
        r = self.client.get(self.url, dict(search='bye'))
        assert r.status_code == 200
        assert pq(r.content)('.no-results').length == 1

    def test_search_author_exists(self):
        """Search by author."""
        self.make_approvals()
        self.make_an_approval(amo.LOG.REQUEST_SUPER_REVIEW, username='editor',
                              comment='hi')

        r = self.client.get(self.url, dict(search='editor'))
        assert r.status_code == 200
        rows = pq(r.content)('#log-listing tbody tr')

        assert rows.filter(':not(.hide)').length == 1
        assert rows.filter('.hide').eq(0).text() == 'hi'

    def test_search_author_case_exists(self):
        """Search by author, with case."""
        self.make_approvals()
        self.make_an_approval(amo.LOG.REQUEST_SUPER_REVIEW, username='editor',
                              comment='hi')

        r = self.client.get(self.url, dict(search='EdItOr'))
        assert r.status_code == 200
        rows = pq(r.content)('#log-listing tbody tr')

        assert rows.filter(':not(.hide)').length == 1
        assert rows.filter('.hide').eq(0).text() == 'hi'

    def test_search_author_doesnt_exist(self):
        """Search by author, with no results."""
        self.make_approvals()
        self.make_an_approval(amo.LOG.REQUEST_SUPER_REVIEW, username='editor')

        r = self.client.get(self.url, dict(search='wrong'))
        assert r.status_code == 200
        assert pq(r.content)('.no-results').length == 1

    def test_search_addon_exists(self):
        """Search by add-on name."""
        self.make_approvals()
        addon = Addon.objects.all()[0]
        r = self.client.get(self.url, dict(search=addon.name))
        assert r.status_code == 200
        tr = pq(r.content)('#log-listing tr[data-addonid="%s"]' % addon.id)
        assert tr.length == 1
        assert tr.siblings('.comments').text() == 'youwin'

    def test_search_addon_case_exists(self):
        """Search by add-on name, with case."""
        self.make_approvals()
        addon = Addon.objects.all()[0]
        r = self.client.get(self.url, dict(search=str(addon.name).swapcase()))
        assert r.status_code == 200
        tr = pq(r.content)('#log-listing tr[data-addonid="%s"]' % addon.id)
        assert tr.length == 1
        assert tr.siblings('.comments').text() == 'youwin'

    def test_search_addon_doesnt_exist(self):
        """Search by add-on name, with no results."""
        self.make_approvals()
        r = self.client.get(self.url, dict(search='xxx'))
        assert r.status_code == 200
        assert pq(r.content)('.no-results').length == 1

    def test_breadcrumbs(self):
        self._test_breadcrumbs([('Add-on Review Log', None)])

    @patch('olympia.devhub.models.ActivityLog.arguments', new=Mock)
    def test_addon_missing(self):
        self.make_approvals()
        r = self.client.get(self.url)
        assert pq(r.content)('#log-listing tr td').eq(1).text() == (
            'Add-on has been deleted.')

    def test_request_info_logs(self):
        self.make_an_approval(amo.LOG.REQUEST_INFORMATION)
        r = self.client.get(self.url)
        assert pq(r.content)('#log-listing tr td a').eq(1).text() == (
            'needs more information')

    def test_super_review_logs(self):
        self.make_an_approval(amo.LOG.REQUEST_SUPER_REVIEW)
        r = self.client.get(self.url)
        assert pq(r.content)('#log-listing tr td a').eq(1).text() == (
            'needs super review')

    def test_comment_logs(self):
        self.make_an_approval(amo.LOG.COMMENT_VERSION)
        r = self.client.get(self.url)
        assert pq(r.content)('#log-listing tr td a').eq(1).text() == (
            'commented')


class TestHome(EditorTest):
    fixtures = EditorTest.fixtures + ['base/addon_3615']

    def setUp(self):
        super(TestHome, self).setUp()
        self.login_as_editor()
        self.url = reverse('editors.home')
        self.user = UserProfile.objects.get(id=5497308)
        self.user.display_name = 'editor'
        self.user.save()
        amo.set_user(self.user)

    def approve_reviews(self):
        amo.set_user(self.user)
        for addon in Addon.objects.all():
            amo.log(amo.LOG['APPROVE_VERSION'], addon, addon.current_version)

    def delete_review(self):
        review = self.make_review()
        review.delete()
        amo.log(amo.LOG.DELETE_REVIEW, review.addon, review,
                details=dict(addon_title='test', title='foo', body='bar',
                             is_flagged=True))
        return review

    def test_approved_review(self):
        review = self.make_review()
        amo.log(amo.LOG.APPROVE_REVIEW, review, review.addon,
                details=dict(addon_name='test', addon_id=review.addon.pk,
                             is_flagged=True))
        r = self.client.get(self.url)
        row = pq(r.content)('.row')
        assert 'approved' in row.text(), (
            'Expected review to be approved by editor')
        assert row('a[href*=yermom]'), 'Expected links to approved addon'

    def test_deleted_review(self):
        self.delete_review()
        doc = pq(self.client.get(self.url).content)

        assert doc('.row').eq(0).text().strip().split('.')[0] == (
            'editor deleted Review for yermom')

        al_id = ActivityLog.objects.all()[0].id
        url = reverse('editors.eventlog.detail', args=[al_id])
        doc = pq(self.client.get(url).content)

        elems = zip(doc('dt'), doc('dd'))
        expected = [
            ('Add-on Title', 'test'),
            ('Review Title', 'foo'),
            ('Review Text', 'bar'),
        ]
        for (dt, dd), texts in zip(elems, expected):
            assert dt.text == texts[0]
            assert dd.text == texts[1]

    def undelete_review(self, review, allowed):
        al = ActivityLog.objects.order_by('-id')[0]
        assert al.arguments[1] == review

        url = reverse('editors.eventlog.detail', args=[al.id])
        doc = pq(self.client.get(url).content)

        assert allowed == (
            doc('#submit-undelete-review').attr('value') == 'Undelete')

        r = self.client.post(url, {'action': 'undelete'})
        assert r.status_code in (302, 403)
        post = r.status_code == 302

        assert post == allowed

    def test_undelete_review_own(self):
        review = self.delete_review()
        # Undeleting a review you deleted is always allowed.
        self.undelete_review(review, allowed=True)

    def test_undelete_review_other(self):
        amo.set_user(UserProfile.objects.get(email='admin@mozilla.com'))
        review = self.delete_review()

        # Normal editors undeleting reviews deleted by other editors is
        # not allowed.
        amo.set_user(self.user)
        self.undelete_review(review, allowed=False)

    def test_undelete_review_admin(self):
        review = self.delete_review()

        # Admins can always undelete reviews.
        self.login_as_admin()
        self.undelete_review(review, allowed=True)

    def test_stats_total(self):
        self.approve_reviews()

        doc = pq(self.client.get(self.url).content)

        cols = doc('#editors-stats .editor-stats-table').eq(1).find('td')
        assert cols.eq(0).text() == self.user.display_name
        assert int(cols.eq(1).text()) == 2  # Approval count should be 2.

    def test_stats_total_admin(self):
        self.login_as_admin()
        self.user = UserProfile.objects.get(email='admin@mozilla.com')
        amo.set_user(self.user)

        create_addon_file('No admin review', version_str='1.0',
                          addon_status=amo.STATUS_NOMINATED,
                          file_status=amo.STATUS_AWAITING_REVIEW)
        create_addon_file('Admin review', version_str='1.0',
                          addon_status=amo.STATUS_NOMINATED, admin_review=True,
                          file_status=amo.STATUS_AWAITING_REVIEW)

        doc = pq(self.client.get(self.url).content)
        tooltip = doc('.editor-stats-table').eq(0).find('.waiting_new')
        assert '2 add-ons' in tooltip.attr('title')

    def test_stats_monthly(self):
        self.approve_reviews()

        doc = pq(self.client.get(self.url).content)

        cols = doc('#editors-stats .editor-stats-table').eq(1).find('td')
        assert cols.eq(0).text() == self.user.display_name
        assert int(cols.eq(1).text()) == 2  # Approval count should be 2.

    @override_settings(EDITOR_REVIEWS_MAX_DISPLAY=0)
    def test_stats_user_position_ranked(self):
        self.approve_reviews()
        doc = pq(self.client.get(self.url).content)
        el = doc('#editors-stats .editor-stats-table').eq(0)('div:last-child')
        assert el.text() == "You're #1 with 2 reviews"  # Total, all time.
        el = doc('#editors-stats .editor-stats-table').eq(1)('div:last-child')
        assert el.text() == "You're #1 with 2 reviews"  # Monthly.

    def test_stats_user_position_unranked(self):
        self.approve_reviews()
        doc = pq(self.client.get(self.url).content)
        p = doc('#editors-stats .editor-stats-table p').eq(0)
        assert not p.text()
        p = doc('#editors-stats .editor-stats-table p').eq(1)
        # Monthly reviews should not be displayed.
        assert not p.text()

    def test_new_editors(self):
        amo.log(amo.LOG.GROUP_USER_ADDED,
                Group.objects.get(name='Add-on Reviewers'), self.user)

        doc = pq(self.client.get(self.url).content)

        anchors = doc('#editors-stats .editor-stats-table').eq(2).find('td a')
        assert anchors.eq(0).text() == self.user.display_name

    def test_unlisted_queues_only_for_senior_reviewers(self):
        listed_queues_links = [
            reverse('editors.queue_nominated'),
            reverse('editors.queue_pending'),
            reverse('editors.queue_moderated')]
        unlisted_queues_links = [
            reverse('editors.unlisted_queue_all')]

        # Only listed queues for editors.
        doc = pq(self.client.get(self.url).content)
        queues = doc('#listed-queues ul li a')
        queues_links = [link.attrib['href'] for link in queues]
        assert queues_links == listed_queues_links
        assert not doc('#unlisted-queues')  # Unlisted queues are not visible.

        # Both listed and unlisted queues for senior editors.
        self.login_as_senior_editor()
        doc = pq(self.client.get(self.url).content)
        queues = doc('#listed-queues ul li a')  # Listed queues links.
        queues_links = [link.attrib['href'] for link in queues]
        assert queues_links == listed_queues_links
        queues = doc('#unlisted-queues ul li a')  # Unlisted queues links.
        queues_links = [link.attrib['href'] for link in queues]
        assert queues_links == unlisted_queues_links

    def test_stats_listed_unlisted(self):
        # Make sure the listed addons are displayed in the listed stats, and
        # that the unlisted addons are listed in the unlisted stats.
        # Create one listed, and two unlisted.
        create_addon_file('listed', '0.1', amo.STATUS_NOMINATED,
                          amo.STATUS_AWAITING_REVIEW)
        create_addon_file('unlisted 1', '0.1', amo.STATUS_NOMINATED,
                          amo.STATUS_AWAITING_REVIEW, listed=False)
        create_addon_file('unlisted 2', '0.1', amo.STATUS_NOMINATED,
                          amo.STATUS_AWAITING_REVIEW, listed=False)

        selector = '.editor-stats-title'  # The new addons stats header.

        self.login_as_senior_editor()
        doc = pq(self.client.get(self.url).content)
        listed_stats = doc('#editors-stats-charts {0}'.format(selector)).eq(0)
        assert 'New Add-on (1)' in listed_stats.text()


class QueueTest(EditorTest):
    fixtures = ['base/users']
    listed = True

    def setUp(self):
        super(QueueTest, self).setUp()
        if self.listed:
            self.login_as_editor()
        else:  # Testing unlisted views: needs Addons:ReviewUnlisted perm.
            self.login_as_senior_editor()
        self.url = reverse('editors.queue_pending')
        self.addons = SortedDict()
        self.expected_addons = []

    def generate_files(self, subset=None):
        if subset is None:
            subset = []
        files = SortedDict([
            ('Pending One', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_PUBLIC,
                'file_status': amo.STATUS_AWAITING_REVIEW,
            }),
            ('Pending Two', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_PUBLIC,
                'file_status': amo.STATUS_AWAITING_REVIEW,
            }),
            ('Nominated One', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
            }),
            ('Nominated Two', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
            }),
            ('Public', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_PUBLIC,
                'file_status': amo.STATUS_PUBLIC,
            }),
        ])
        results = SortedDict()
        for name, attrs in files.iteritems():
            if not subset or name in subset:
                results[name] = self.addon_file(name, **attrs)
        return results

    def generate_file(self, name):
        return self.generate_files([name])[name]

    def get_review_data(self):
        # Format: (Created n days ago,
        #          percentages of [< 5, 5-10, >10])
        return ((1, (0, 0, 100)),
                (8, (0, 50, 50)),
                (11, (50, 0, 50)))

    def addon_file(self, *args, **kw):
        a = create_addon_file(*args, listed=self.listed, **kw)
        name = args[0]  # Add-on name.
        self.addons[name] = a['addon']
        return a['addon']

    def get_addon_latest_version(self, addon):
        if self.listed:
            channel = amo.RELEASE_CHANNEL_LISTED
        else:
            channel = amo.RELEASE_CHANNEL_UNLISTED
        return addon.find_latest_version(channel=channel)

    def get_queue(self, addon):
        version = self.get_addon_latest_version(addon)
        assert version.current_queue.objects.filter(id=addon.id).count() == 1

    def get_expected_addons_by_names(self, names):
        expected_addons = []
        files = self.generate_files()
        for name in sorted(names):
            if name in files:
                    expected_addons.append(files[name])
        # Make sure all elements have been added
        assert len(expected_addons) == len(names)
        return expected_addons

    def _test_get_queue(self):
        for addon in self.expected_addons:
            self.get_queue(addon)

    def _test_queue_count(self, position, name, count):
        r = self.client.get(self.url)
        assert r.status_code == 200
        a = pq(r.content)('.tabnav li a').eq(position)
        assert a.text() == '%s (%s)' % (name, count)
        assert a.attr('href') == self.url

    def _test_results(self):
        r = self.client.get(self.url)
        assert r.status_code == 200
        expected = []
        if not len(self.expected_addons):
            raise AssertionError('self.expected_addons was an empty list')
        for idx, addon in enumerate(self.expected_addons):
            latest_version = self.get_addon_latest_version(addon)
            assert latest_version
            name = '%s %s' % (unicode(addon.name),
                              latest_version.version)
            channel = ['unlisted'] if not self.listed else []
            url = reverse('editors.review', args=channel + [addon.slug])
            expected.append((name, url))
        check_links(
            expected,
            pq(r.content)('#addon-queue tr.addon-row td a:not(.app-icon)'),
            verify=False)


class TestQueueBasics(QueueTest):
    fixtures = QueueTest.fixtures + ['editors/user_persona_reviewer']

    def test_only_viewable_by_editor(self):
        # Addon reviewer has access.
        r = self.client.get(self.url)
        assert r.status_code == 200

        # Regular user doesn't have access.
        self.client.logout()
        assert self.client.login(email='regular@mozilla.com')
        r = self.client.get(self.url)
        assert r.status_code == 403

        # Persona reviewer doesn't have access either.
        self.client.logout()
        assert self.client.login(email='persona_reviewer@mozilla.com')
        r = self.client.get(self.url)
        assert r.status_code == 403

    def test_invalid_page(self):
        r = self.client.get(self.url, {'page': 999})
        assert r.status_code == 200
        assert r.context['page'].number == 1

    def test_invalid_per_page(self):
        r = self.client.get(self.url, {'per_page': '<garbage>'})
        # No exceptions:
        assert r.status_code == 200

    def test_grid_headers(self):
        r = self.client.get(self.url)
        assert r.status_code == 200
        doc = pq(r.content)
        expected = [
            'Add-on',
            'Type',
            'Waiting Time',
            'Flags',
        ]
        assert [pq(th).text() for th in doc('#addon-queue tr th')[1:]] == (
            expected)

    def test_grid_headers_sort_after_search(self):
        params = dict(searching=['True'],
                      text_query=['abc'],
                      addon_type_ids=['2'],
                      sort=['addon_type_id'])
        r = self.client.get(self.url, params)
        assert r.status_code == 200
        tr = pq(r.content)('#addon-queue tr')
        sorts = {
            # Column index => sort.
            1: 'addon_name',        # Add-on.
            2: '-addon_type_id',    # Type.
            3: 'waiting_time_min',  # Waiting Time.
        }
        for idx, sort in sorts.iteritems():
            # Get column link.
            a = tr('th').eq(idx).find('a')
            # Update expected GET parameters with sort type.
            params.update(sort=[sort])
            # Parse querystring of link to make sure `sort` type is correct.
            assert urlparse.parse_qs(a.attr('href').split('?')[1]) == params

    def test_no_results(self):
        r = self.client.get(self.url)
        assert r.status_code == 200
        assert pq(r.content)('.queue-outer .no-results').length == 1

    def test_no_paginator_when_on_single_page(self):
        r = self.client.get(self.url)
        assert r.status_code == 200
        assert pq(r.content)('.pagination').length == 0

    def test_paginator_when_many_pages(self):
        # 'Pending One' and 'Pending Two' should be the only add-ons in
        # the pending queue, but we'll generate them all for good measure.
        self.generate_files()

        r = self.client.get(self.url, {'per_page': 1})
        assert r.status_code == 200
        doc = pq(r.content)
        assert doc('.data-grid-top .num-results').text() == (
            u'Results 1 \u2013 1 of 2')
        assert doc('.data-grid-bottom .num-results').text() == (
            u'Results 1 \u2013 1 of 2')

    def test_navbar_queue_counts(self):
        self.generate_files()

        r = self.client.get(self.url)
        assert r.status_code == 200
        doc = pq(r.content)
        assert doc('#navbar li.top ul').eq(0).text() == (
            'New Add-ons (2) Updates (2) Moderated Reviews (0)')

    def test_legacy_queue_sort(self):
        sorts = (
            ['age', 'Waiting Time'],
            ['name', 'Add-on'],
            ['type', 'Type'],
        )
        for key, text in sorts:
            r = self.client.get(self.url, {'sort': key})
            assert r.status_code == 200
            assert pq(r.content)('th.ordered a').text() == text

    def test_full_reviews_bar(self):
        self.generate_files()

        addon = self.addons['Nominated Two']
        for data in self.get_review_data():
            self.check_bar(addon, eq=0, data=data, reset_status=False)

    def test_pending_bar(self):
        self.generate_files()

        addon = self.addons['Pending One']
        for data in self.get_review_data():
            self.check_bar(addon, eq=1, data=data, reset_status=True)

    def check_bar(self, addon, eq, data, reset_status=False):
        # `eq` is the table number (0, 1 or 2).
        def style(w):
            return 'width:%s%%' % (float(w) if w > 0 else 0)

        days, widths = data

        f = addon.versions.all()[0].all_files[0]
        d = datetime.now() - timedelta(days=days)
        f.update(created=d)
        addon.versions.latest().update(nomination=d)

        # For pending, we must reset the add-on status after saving version.
        if reset_status:
            addon.update(status=amo.STATUS_PUBLIC)

        r = self.client.get(reverse('editors.home'))
        doc = pq(r.content)

        sel = '#editors-stats-charts{0}'.format('' if self.listed
                                                else '-unlisted')
        div = doc('{0} .editor-stats-table'.format(sel)).eq(eq)

        assert div('.waiting_old').attr('style') == style(widths[0])
        assert div('.waiting_med').attr('style') == style(widths[1])
        assert div('.waiting_new').attr('style') == style(widths[2])

    def test_flags_jetpack(self):
        ad = create_addon_file('Jetpack', '0.1', amo.STATUS_NOMINATED,
                               amo.STATUS_AWAITING_REVIEW)
        ad_file = ad['version'].files.all()[0]
        ad_file.update(jetpack_version=1.2)

        r = self.client.get(reverse('editors.queue_nominated'))

        rows = pq(r.content)('#addon-queue tr.addon-row')
        assert rows.length == 1
        assert rows.attr('data-addon') == str(ad['addon'].id)
        assert rows.find('td').eq(1).text() == 'Jetpack 0.1'
        assert rows.find('.ed-sprite-jetpack').length == 1

    def test_flags_requires_restart(self):
        ad = create_addon_file('Some Add-on', '0.1', amo.STATUS_NOMINATED,
                               amo.STATUS_AWAITING_REVIEW,
                               file_kw={'no_restart': False})

        r = self.client.get(reverse('editors.queue_nominated'))

        rows = pq(r.content)('#addon-queue tr.addon-row')
        assert rows.length == 1
        assert rows.attr('data-addon') == str(ad['addon'].id)
        assert rows.find('td').eq(1).text() == 'Some Add-on 0.1'
        assert rows.find('.ed-sprite-jetpack').length == 0
        assert rows.find('.ed-sprite-requires_restart').length == 1

    def test_flags_no_restart(self):
        # create_addon_file() creates restartless files by default.
        ad = create_addon_file('Restartless', '0.1',
                               amo.STATUS_NOMINATED,
                               amo.STATUS_AWAITING_REVIEW)

        r = self.client.get(reverse('editors.queue_nominated'))

        rows = pq(r.content)('#addon-queue tr.addon-row')
        assert rows.length == 1
        assert rows.attr('data-addon') == str(ad['addon'].id)
        assert rows.find('td').eq(1).text() == 'Restartless 0.1'
        assert rows.find('.ed-sprite-jetpack').length == 0
        assert rows.find('.ed-sprite-requires_restart').length == 0

    def test_theme_redirect(self):
        users = []
        for x in range(2):
            user = amo.tests.user_factory()
            user.save()
            users.append(user)

        self.grant_permission(users[0], 'Personas:Review')
        self.client.logout()
        self.login(users[0])
        res = self.client.get(reverse('editors.home'))
        self.assert3xx(res, reverse('editors.themes.home'))

        self.grant_permission(users[1], 'Addons:Review')
        self.client.logout()
        self.login(users[1])
        res = self.client.get(reverse('editors.home'))
        assert res.status_code == 200


class TestPendingQueue(QueueTest):

    def setUp(self):
        super(TestPendingQueue, self).setUp()
        # These should be the only ones present.
        self.expected_addons = self.get_expected_addons_by_names(
            ['Pending One', 'Pending Two'])
        self.url = reverse('editors.queue_pending')

    def test_results(self):
        self._test_results()

    def test_breadcrumbs(self):
        self._test_breadcrumbs([('Updates', None)])

    def test_queue_count(self):
        self._test_queue_count(1, 'Updates', 2)

    def test_get_queue(self):
        self._test_get_queue()


class TestNominatedQueue(QueueTest):

    def setUp(self):
        super(TestNominatedQueue, self).setUp()
        # These should be the only ones present.
        self.expected_addons = self.get_expected_addons_by_names(
            ['Nominated One', 'Nominated Two'])
        self.url = reverse('editors.queue_nominated')

    def test_results(self):
        self._test_results()

    def test_breadcrumbs(self):
        self._test_breadcrumbs([('New Add-ons', None)])

    def test_results_two_versions(self):
        version1 = self.addons['Nominated One'].versions.all()[0]
        version2 = self.addons['Nominated Two'].versions.all()[0]
        file_ = version2.files.get()

        # Versions are ordered by creation date, so make sure they're set.
        past = self.days_ago(1)
        version2.update(created=past, nomination=past)

        # Create another version, v0.2, by "cloning" v0.1.
        version2.pk = None
        version2.version = '0.2'
        version2.save()

        # Reset creation date once it has been saved.
        future = datetime.now() - timedelta(seconds=1)
        version2.update(created=future, nomination=future)

        # Associate v0.2 it with a file.
        file_.pk = None
        file_.version = version2
        file_.save()

        # disable old files like Version.from_upload() would.
        version2.disable_old_files()

        r = self.client.get(self.url)
        assert r.status_code == 200
        expected = [
            ('Nominated One 0.1', reverse('editors.review',
                                          args=[version1.addon.slug])),
            ('Nominated Two 0.2', reverse('editors.review',
                                          args=[version2.addon.slug])),
        ]
        check_links(
            expected,
            pq(r.content)('#addon-queue tr.addon-row td a:not(.app-icon)'),
            verify=False)

    def test_queue_count(self):
        self._test_queue_count(0, 'New Add-ons', 2)

    def test_get_queue(self):
        self._test_get_queue()


class TestModeratedQueue(QueueTest):
    fixtures = ['base/users', 'reviews/dev-reply']

    def setUp(self):
        super(TestModeratedQueue, self).setUp()

        self.url = reverse('editors.queue_moderated')
        url_flag = reverse('addons.reviews.flag', args=['a1865', 218468])

        response = self.client.post(url_flag, {'flag': ReviewFlag.SPAM})
        assert response.status_code == 200

        assert ReviewFlag.objects.filter(flag=ReviewFlag.SPAM).count() == 1
        assert Review.objects.filter(editorreview=True).count() == 1

    def test_results(self):
        r = self.client.get(self.url)
        assert r.status_code == 200
        doc = pq(r.content)('#reviews-flagged')

        rows = doc('.review-flagged:not(.review-saved)')
        assert rows.length == 1
        assert rows.find('h3').text() == ": Don't use Firefox 2.0!"

        # Default is "Skip."
        assert doc('#id_form-0-action_1:checked').length == 1

        flagged = doc('.reviews-flagged-reasons span.light').text()
        editor = ReviewFlag.objects.all()[0].user.name
        assert flagged.startswith('Flagged by %s' % editor), (
            'Unexpected text: %s' % flagged)

    def setup_actions(self, action):
        ctx = self.client.get(self.url).context
        fs = initial(ctx['reviews_formset'].forms[0])

        assert Review.objects.filter(addon=1865).count() == 2

        data_formset = formset(fs)
        data_formset['form-0-action'] = action

        r = self.client.post(self.url, data_formset)
        self.assert3xx(r, self.url)

    def test_skip(self):
        self.setup_actions(reviews.REVIEW_MODERATE_SKIP)

        # Make sure it's still there.
        r = self.client.get(self.url)
        doc = pq(r.content)
        rows = doc('#reviews-flagged .review-flagged:not(.review-saved)')
        assert rows.length == 1

    def test_skip_score(self):
        self.setup_actions(reviews.REVIEW_MODERATE_SKIP)
        assert ReviewerScore.objects.filter(
            note_key=amo.REVIEWED_ADDON_REVIEW).count() == 0

    def get_logs(self, action):
        return ActivityLog.objects.filter(action=action.id)

    def test_remove(self):
        """Make sure the editor tools can delete a review."""
        self.setup_actions(reviews.REVIEW_MODERATE_DELETE)
        logs = self.get_logs(amo.LOG.DELETE_REVIEW)
        assert logs.count() == 1

        # Make sure it's removed from the queue.
        r = self.client.get(self.url)
        assert pq(r.content)('#reviews-flagged .no-results').length == 1

        r = self.client.get(reverse('editors.eventlog'))
        assert pq(r.content)('table .more-details').attr('href') == (
            reverse('editors.eventlog.detail', args=[logs[0].id]))

        # Make sure it was actually deleted.
        assert Review.objects.filter(addon=1865).count() == 1
        # But make sure it wasn't *actually* deleted.
        assert Review.unfiltered.filter(addon=1865).count() == 2

    def test_remove_fails_for_own_addon(self):
        """
        Make sure the editor tools can't delete a review for an
        add-on owned by the user.
        """
        a = Addon.objects.get(pk=1865)
        u = UserProfile.objects.get(email='editor@mozilla.com')
        AddonUser(addon=a, user=u).save()

        # Make sure the initial count is as expected
        assert Review.objects.filter(addon=1865).count() == 2

        self.setup_actions(reviews.REVIEW_MODERATE_DELETE)
        logs = self.get_logs(amo.LOG.DELETE_REVIEW)
        assert logs.count() == 0

        # Make sure it's not removed from the queue.
        r = self.client.get(self.url)
        assert pq(r.content)('#reviews-flagged .no-results').length == 0

        # Make sure it was not actually deleted.
        assert Review.objects.filter(addon=1865).count() == 2

    def test_remove_score(self):
        self.setup_actions(reviews.REVIEW_MODERATE_DELETE)
        assert ReviewerScore.objects.filter(
            note_key=amo.REVIEWED_ADDON_REVIEW).count() == 1

    def test_keep(self):
        """Make sure the editor tools can remove flags and keep a review."""
        self.setup_actions(reviews.REVIEW_MODERATE_KEEP)
        logs = self.get_logs(amo.LOG.APPROVE_REVIEW)
        assert logs.count() == 1

        # Make sure it's removed from the queue.
        r = self.client.get(self.url)
        assert pq(r.content)('#reviews-flagged .no-results').length == 1

        review = Review.objects.filter(addon=1865)

        # Make sure it's NOT deleted...
        assert review.count() == 2

        # ...but it's no longer flagged.
        assert review.filter(editorreview=1).count() == 0

    def test_keep_score(self):
        self.setup_actions(reviews.REVIEW_MODERATE_KEEP)
        assert ReviewerScore.objects.filter(
            note_key=amo.REVIEWED_ADDON_REVIEW).count() == 1

    def test_queue_count(self):
        self._test_queue_count(2, 'Moderated Review', 1)

    def test_breadcrumbs(self):
        self._test_breadcrumbs([('Moderated Reviews', None)])

    def test_no_reviews(self):
        Review.objects.all().delete()

        r = self.client.get(self.url)
        assert r.status_code == 200
        doc = pq(r.content)('#reviews-flagged')

        assert doc('.no-results').length == 1
        assert doc('.review-saved button').length == 1  # Show only one button.

    def test_do_not_show_reviews_for_non_public_addons(self):
        Addon.objects.all().update(status=amo.STATUS_NULL)

        res = self.client.get(self.url)
        assert res.status_code == 200
        doc = pq(res.content)('#reviews-flagged')

        # There should be no results since all add-ons are not public.
        assert doc('.no-results').length == 1

    def test_do_not_show_reviews_for_unlisted_addons(self):
        for addon in Addon.objects.all():
            self.make_addon_unlisted(addon)

        res = self.client.get(self.url)
        assert res.status_code == 200
        doc = pq(res.content)('#reviews-flagged')

        # There should be no results since all add-ons are unlisted.
        assert doc('.no-results').length == 1


class TestUnlistedAllList(QueueTest):
    listed = False

    def setUp(self):
        super(TestUnlistedAllList, self).setUp()
        self.url = reverse('editors.unlisted_queue_all')
        # We should have all add-ons.
        self.expected_addons = self.get_expected_addons_by_names(
            ['Pending One', 'Pending Two', 'Nominated One', 'Nominated Two',
             'Public'])
        # Need to set unique nomination times or we get a psuedo-random order.
        for idx, addon in enumerate(self.expected_addons):
            latest_version = addon.find_latest_version(
                channel=amo.RELEASE_CHANNEL_UNLISTED)
            latest_version.update(
                nomination=(datetime.now() - timedelta(minutes=idx)))

    def test_breadcrumbs(self):
        self._test_breadcrumbs([('All Unlisted Add-ons', None)])

    def test_results(self):
        self._test_results()

    def test_review_notes_json(self):
        latest_version = self.expected_addons[0].find_latest_version(
            channel=amo.RELEASE_CHANNEL_UNLISTED)
        log = amo.log(amo.LOG.APPROVE_VERSION,
                      latest_version,
                      self.expected_addons[0],
                      user=UserProfile.objects.get(pk=999),
                      details={'comments': 'stish goin` down son'})
        url = reverse('editors.queue_review_text') + str(log.id)
        r = self.client.get(url)
        assert json.loads(r.content) == {'reviewtext': 'stish goin` down son'}


class TestPerformance(QueueTest):
    fixtures = ['base/users', 'editors/pending-queue', 'base/addon_3615']

    """Test the page at /editors/performance."""

    def setUpEditor(self):
        self.login_as_editor()
        amo.set_user(UserProfile.objects.get(username='editor'))
        self.create_logs()

    def setUpSeniorEditor(self):
        self.login_as_senior_editor()
        amo.set_user(UserProfile.objects.get(username='senioreditor'))
        self.create_logs()

    def setUpAdmin(self):
        self.login_as_admin()
        amo.set_user(UserProfile.objects.get(username='admin'))
        self.create_logs()

    def get_url(self, args=None):
        if args is None:
            args = []
        return reverse('editors.performance', args=args)

    def create_logs(self):
        addon = Addon.objects.all()[0]
        version = addon.versions.all()[0]
        for i in amo.LOG_EDITOR_REVIEW_ACTION:
            amo.log(amo.LOG_BY_ID[i], addon, version)

    def _test_chart(self):
        r = self.client.get(self.get_url())
        assert r.status_code == 200
        doc = pq(r.content)

        num = len(amo.LOG_EDITOR_REVIEW_ACTION)
        label = datetime.now().strftime('%Y-%m')
        data = {label: {u'teamcount': num, u'teamavg': u'%s.0' % num,
                        u'usercount': num, u'teamamt': 1,
                        u'label': datetime.now().strftime('%b %Y')}}

        assert json.loads(doc('#monthly').attr('data-chart')) == data

    def test_performance_chart_editor(self):
        self.setUpEditor()
        self._test_chart()

    def test_performance_chart_as_senior_editor(self):
        self.setUpSeniorEditor()
        self._test_chart()

    def test_performance_chart_as_admin(self):
        self.setUpAdmin()
        self._test_chart()

    def test_usercount_with_more_than_one_editor(self):
        self.client.login(email='clouserw@gmail.com')
        amo.set_user(UserProfile.objects.get(username='clouserw'))
        self.create_logs()
        self.setUpEditor()
        r = self.client.get(self.get_url())
        assert r.status_code == 200
        doc = pq(r.content)
        data = json.loads(doc('#monthly').attr('data-chart'))
        label = datetime.now().strftime('%Y-%m')
        assert data[label]['usercount'] == len(amo.LOG_EDITOR_REVIEW_ACTION)

    def _test_performance_other_user_as_admin(self):
        userid = amo.get_user().pk

        r = self.client.get(self.get_url([10482]))
        doc = pq(r.content)

        assert doc('#select_user').length == 1  # Let them choose editors.
        options = doc('#select_user option')
        assert options.length == 3
        assert options.eq(2).val() == str(userid)

        assert 'clouserw' in doc('#reviews_user').text()

    def test_performance_other_user_as_admin(self):
        self.setUpAdmin()

        self._test_performance_other_user_as_admin()

    def test_performance_other_user_as_senior_editor(self):
        self.setUpSeniorEditor()

        self._test_performance_other_user_as_admin()

    def test_performance_other_user_not_admin(self):
        self.setUpEditor()

        r = self.client.get(self.get_url([10482]))
        doc = pq(r.content)

        assert doc('#select_user').length == 0  # Don't let them choose.
        assert doc('#reviews_user').text() == 'Your Reviews'


class SearchTest(EditorTest):
    listed = True

    def setUp(self):
        super(SearchTest, self).setUp()
        if self.listed:
            self.login_as_editor()
        else:  # Testing unlisted views: needs Addons:ReviewUnlisted perm.
            self.login_as_senior_editor()

    def named_addons(self, request):
        return [
            r.record.addon_name for r in request.context['page'].object_list]

    def search(self, *args, **kw):
        r = self.client.get(self.url, kw)
        assert r.status_code == 200
        assert r.context['search_form'].errors.as_text() == ''
        return r


class BaseTestQueueSearch(SearchTest):
    fixtures = ['base/users', 'base/appversion']
    __test__ = False  # this is an abstract test case

    def generate_files(self, subset=None):
        if subset is None:
            subset = []
        files = SortedDict([
            ('Not Admin Reviewed', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
            }),
            ('Another Not Admin Reviewed', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
            }),
            ('Admin Reviewed', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
                'admin_review': True,
            }),
            ('Justin Bieber Theme', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
                'addon_type': amo.ADDON_THEME,
            }),
            ('Justin Bieber Search Bar', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
                'addon_type': amo.ADDON_SEARCH,
            }),
            ('Bieber For Mobile', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
                'application': amo.MOBILE,
            }),
            ('Linux Widget', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
            }),
            ('Mac Widget', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
            }),
            ('Deleted', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_DELETED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
            }),
        ])
        results = {}
        for name, attrs in files.iteritems():
            if not subset or name in subset:
                results[name] = create_addon_file(name, listed=self.listed,
                                                  **attrs)
        return results

    def generate_file(self, name):
        return self.generate_files([name])[name]

    def test_search_by_admin_reviewed_admin(self):
        self.login_as_admin()
        self.generate_files(['Not Admin Reviewed', 'Admin Reviewed'])
        r = self.search(admin_review=1)
        assert self.named_addons(r) == ['Admin Reviewed']

    def test_queue_counts_admin(self):
        self.login_as_admin()
        self.generate_files(['Not Admin Reviewed', 'Admin Reviewed'])
        r = self.search(text_query='admin', per_page=1)
        doc = pq(r.content)
        assert doc('.data-grid-top .num-results').text() == (
            u'Results 1 \u2013 1 of 2')

    def test_search_by_addon_name_admin(self):
        self.login_as_admin()
        self.generate_files(['Not Admin Reviewed', 'Admin Reviewed',
                             'Justin Bieber Theme'])
        r = self.search(text_query='admin')
        assert sorted(self.named_addons(r)) == [
            'Admin Reviewed', 'Not Admin Reviewed']

    def test_not_searching(self):
        self.generate_files(['Not Admin Reviewed', 'Admin Reviewed'])
        r = self.search()
        assert sorted(self.named_addons(r)) == ['Not Admin Reviewed']

    def test_search_by_nothing(self):
        self.generate_files(['Not Admin Reviewed', 'Admin Reviewed'])
        r = self.search(searching='True')
        assert sorted(self.named_addons(r)) == (
            ['Admin Reviewed', 'Not Admin Reviewed'])

    def test_search_by_admin_reviewed(self):
        self.generate_files(['Not Admin Reviewed', 'Admin Reviewed'])
        r = self.search(admin_review=1, searching='True')
        assert self.named_addons(r) == ['Admin Reviewed']

    def test_queue_counts(self):
        self.generate_files(['Not Admin Reviewed',
                             'Another Not Admin Reviewed', 'Admin Reviewed'])
        r = self.search(text_query='admin', per_page=1, searching='True')
        doc = pq(r.content)
        assert doc('.data-grid-top .num-results').text() == (
            u'Results 1 \u2013 1 of 3')

    def test_search_by_addon_name(self):
        self.generate_files(['Not Admin Reviewed', 'Admin Reviewed',
                             'Justin Bieber Theme'])
        r = self.search(text_query='admin', searching='True')
        assert sorted(self.named_addons(r)) == (
            ['Admin Reviewed', 'Not Admin Reviewed'])

    def test_search_by_addon_in_locale(self):
        name = 'Not Admin Reviewed'
        d = self.generate_file(name)
        uni = 'フォクすけといっしょ'.decode('utf8')
        a = Addon.objects.get(pk=d['addon'].id)
        a.name = {'ja': uni}
        a.save()
        r = self.client.get('/ja/' + self.url, {'text_query': uni},
                            follow=True)
        assert r.status_code == 200
        assert self.named_addons(r) == [name]

    def test_search_by_addon_author(self):
        name = 'Not Admin Reviewed'
        d = self.generate_file(name)
        u = UserProfile.objects.all()[0]
        email = u.email.swapcase()
        author = AddonUser.objects.create(user=u, addon=d['addon'])
        for role in [amo.AUTHOR_ROLE_OWNER, amo.AUTHOR_ROLE_DEV]:
            author.role = role
            author.save()
            r = self.search(text_query=email)
            assert self.named_addons(r) == [name]
        author.role = amo.AUTHOR_ROLE_VIEWER
        author.save()
        r = self.search(text_query=email)
        assert self.named_addons(r) == []

    def test_search_by_supported_email_in_locale(self):
        name = 'Not Admin Reviewed'
        d = self.generate_file(name)
        uni = 'フォクすけといっしょ@site.co.jp'.decode('utf8')
        a = Addon.objects.get(pk=d['addon'].id)
        a.support_email = {'ja': uni}
        a.save()
        r = self.client.get('/ja/' + self.url, {'text_query': uni},
                            follow=True)
        assert r.status_code == 200
        assert self.named_addons(r) == [name]

    def test_clear_search_visible(self):
        r = self.search(text_query='admin', searching=True)
        assert r.status_code == 200
        assert pq(r.content)('.clear-queue-search').text() == 'clear search'

    def test_clear_search_hidden(self):
        r = self.search(text_query='admin')
        assert r.status_code == 200
        assert not pq(r.content)('.clear-queue-search').text()


class TestQueueSearch(BaseTestQueueSearch):
    def setUp(self):
        super(TestQueueSearch, self).setUp()
        self.url = reverse('editors.queue_nominated')

    def test_search_by_addon_type(self):
        self.generate_files(['Not Admin Reviewed', 'Justin Bieber Theme',
                             'Justin Bieber Search Bar'])
        r = self.search(addon_type_ids=[amo.ADDON_THEME])
        assert self.named_addons(r) == ['Justin Bieber Theme']

    def test_search_by_addon_type_any(self):
        self.generate_file('Not Admin Reviewed')
        r = self.search(addon_type_ids=[amo.ADDON_ANY])
        assert self.named_addons(r), 'Expected some add-ons'

    def test_search_by_many_addon_types(self):
        self.generate_files(['Not Admin Reviewed', 'Justin Bieber Theme',
                             'Justin Bieber Search Bar'])
        r = self.search(addon_type_ids=[amo.ADDON_THEME,
                                        amo.ADDON_SEARCH])
        assert sorted(self.named_addons(r)) == (
            ['Justin Bieber Search Bar', 'Justin Bieber Theme'])

    def test_search_by_app(self):
        self.generate_files(['Bieber For Mobile', 'Linux Widget'])
        r = self.search(application_id=[amo.MOBILE.id])
        assert r.status_code == 200
        assert self.named_addons(r) == ['Bieber For Mobile']

    def test_preserve_multi_apps(self):
        self.generate_files(['Bieber For Mobile', 'Linux Widget'])
        for app in (amo.MOBILE, amo.FIREFOX):
            create_addon_file('Multi Application', '0.1',
                              amo.STATUS_NOMINATED, amo.STATUS_AWAITING_REVIEW,
                              application=app, listed=self.listed)

        r = self.search(application_id=[amo.MOBILE.id])
        doc = pq(r.content)
        td = doc('#addon-queue tr').eq(2).children('td').eq(5)
        assert td.children().length == 2
        assert td.children('.ed-sprite-firefox').length == 1
        assert td.children('.ed-sprite-mobile').length == 1

    def test_clear_search_uses_correct_queue(self):
        # The "clear search" link points to the right listed or unlisted queue.
        # Listed queue.
        url = reverse('editors.queue_nominated')
        r = self.client.get(url, {'text_query': 'admin', 'searching': True})
        assert pq(r.content)('.clear-queue-search').attr('href') == url


class TestQueueSearchUnlistedAllList(BaseTestQueueSearch):
    listed = False
    __test__ = True

    def setUp(self):
        super(TestQueueSearchUnlistedAllList, self).setUp()
        self.url = reverse('editors.unlisted_queue_all')

    def test_not_searching(self):
        self.generate_files(['Not Admin Reviewed', 'Admin Reviewed'])
        r = self.search()
        # Because we're logged in as senior editor we see admin reviewed too.
        assert sorted(self.named_addons(r)) == [
            'Admin Reviewed', 'Not Admin Reviewed']

    def test_search_deleted(self):
        self.generate_files(['Not Admin Reviewed', 'Deleted'])
        r = self.search(deleted=1)
        assert self.named_addons(r) == ['Deleted']

    def test_search_not_deleted(self):
        self.generate_files(['Not Admin Reviewed', 'Deleted'])
        r = self.search(deleted=0)
        assert self.named_addons(r) == ['Not Admin Reviewed']

    def test_search_by_guid(self):
        name = 'Not Admin Reviewed'
        addon = self.generate_file(name)['addon']
        addon.update(guid='guidymcguid.com')
        r = self.search(text_query='mcguid')
        assert self.named_addons(r) == ['Not Admin Reviewed']


class ReviewBase(QueueTest):

    def setUp(self):
        super(QueueTest, self).setUp()
        self.login_as_editor()
        self.addons = {}

        self.addon = self.generate_file('Public')
        self.version = self.addon.current_version
        self.file = self.version.files.get()
        self.editor = UserProfile.objects.get(username='editor')
        self.editor.update(display_name=u'An edítor')
        self.senior_editor = UserProfile.objects.get(username='senioreditor')
        self.senior_editor.update(display_name=u'A señor editor')
        self.url = reverse('editors.review', args=[self.addon.slug])

        AddonUser.objects.create(addon=self.addon, user_id=999)

    def get_addon(self):
        return Addon.objects.get(pk=self.addon.pk)

    def get_dict(self, **kw):
        data = {'operating_systems': 'win', 'applications': 'something',
                'comments': 'something'}
        data.update(kw)
        return data


class TestReview(ReviewBase):

    def test_reviewer_required(self):
        assert self.client.head(self.url).status_code == 200

    def test_not_anonymous(self):
        self.client.logout()
        self.assertLoginRedirects(self.client.head(self.url), to=self.url)

    @patch.object(settings, 'ALLOW_SELF_REVIEWS', False)
    def test_not_author(self):
        AddonUser.objects.create(addon=self.addon, user=self.editor)
        assert self.client.head(self.url).status_code == 302

    def test_needs_unlisted_reviewer_for_only_unlisted(self):
        self.addon.versions.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
        assert self.client.head(self.url).status_code == 404
        self.login_as_senior_editor()
        assert self.client.head(self.url).status_code == 200

    def test_dont_need_unlisted_reviewer_for_mixed_channels(self):
        create_addon_file(
            'Public', '9.9', amo.STATUS_PUBLIC, amo.STATUS_PUBLIC,
            version_kw={'channel': amo.RELEASE_CHANNEL_UNLISTED})
        assert self.addon.find_latest_version(
            channel=amo.RELEASE_CHANNEL_UNLISTED)
        assert self.addon.current_version.channel == amo.RELEASE_CHANNEL_LISTED
        assert self.client.head(self.url).status_code == 200
        self.login_as_senior_editor()
        assert self.client.head(self.url).status_code == 200

    def test_not_flags(self):
        self.addon.current_version.files.update(no_restart=True)
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert len(response.context['flags']) == 0

    def test_flag_admin_review(self):
        self.addon.current_version.files.update(no_restart=True)
        self.addon.update(admin_review=True)
        response = self.client.get(self.url)
        assert len(response.context['flags']) == 1

    def test_info_comments_requested(self):
        response = self.client.post(self.url, {'action': 'info'})
        assert response.context['form'].errors['comments'][0] == (
            'This field is required.')

    def test_comment(self):
        response = self.client.post(self.url, {'action': 'comment',
                                               'comments': 'hello sailor'})
        assert response.status_code == 302
        assert len(mail.outbox) == 0

        comment_version = amo.LOG.COMMENT_VERSION
        assert ActivityLog.objects.filter(
            action=comment_version.id).count() == 1

    def test_info_requested(self):
        response = self.client.post(self.url, {'action': 'info',
                                               'comments': 'hello sailor'})
        assert response.status_code == 302
        assert len(mail.outbox) == 1
        self.assertTemplateUsed(response, 'editors/emails/info.ltxt')

    def test_super_review_requested(self):
        response = self.client.post(self.url, {'action': 'super',
                                               'comments': 'hello sailor'})
        assert response.status_code == 302
        assert len(mail.outbox) == 2
        self.assertTemplateUsed(response,
                                'editors/emails/author_super_review.ltxt')
        self.assertTemplateUsed(response, 'editors/emails/super_review.ltxt')

    def test_info_requested_canned_response(self):
        response = self.client.post(self.url, {'action': 'info',
                                               'comments': 'hello sailor',
                                               'canned_response': 'foo'})
        assert response.status_code == 302
        assert len(mail.outbox) == 1
        self.assertTemplateUsed(response, 'editors/emails/info.ltxt')

    def test_notify(self):
        response = self.client.post(self.url, {'action': 'info',
                                               'comments': 'hello sailor',
                                               'notify': True})
        assert response.status_code == 302
        assert EditorSubscription.objects.count() == 1

    def test_no_notify(self):
        response = self.client.post(self.url, {'action': 'info',
                                               'comments': 'hello sailor'})
        assert response.status_code == 302
        assert EditorSubscription.objects.count() == 0

    def test_page_title(self):
        response = self.client.get(self.url)
        assert response.status_code == 200
        doc = pq(response.content)
        assert doc('title').text() == (
            '%s :: Editor Tools :: Add-ons for Firefox' % self.addon.name)

    def test_breadcrumbs(self):
        self.generate_files()
        expected = [
            ('Updates', reverse('editors.queue_pending')),
            (unicode(self.addon.name), None),
        ]
        self._test_breadcrumbs(expected)

    def test_breadcrumbs_unlisted_addons(self):
        self.generate_files()
        self.make_addon_unlisted(self.addon)
        self.login_as_admin()
        expected = [
            ('All Unlisted Add-ons',
             reverse('editors.unlisted_queue_all')),
            (unicode(self.addon.name), None),
        ]
        self.url = reverse('editors.review',
                           args=['unlisted', self.addon.slug])
        self._test_breadcrumbs(expected)

    def test_files_shown(self):
        r = self.client.get(self.url)
        assert r.status_code == 200

        items = pq(r.content)('#review-files .files .file-info')
        assert items.length == 1

        f = self.version.all_files[0]
        expected = [
            ('All Platforms', f.get_url_path('editor')),
            ('Validation',
             reverse('devhub.file_validation', args=[self.addon.slug, f.id])),
            ('Contents', None),
        ]
        check_links(expected, items.find('a'), verify=False)

    def test_item_history(self, channel=amo.RELEASE_CHANNEL_LISTED):
        self.addon_file(u'something', u'0.2', amo.STATUS_PUBLIC,
                        amo.STATUS_AWAITING_REVIEW,
                        version_kw={'channel': channel})
        assert self.addon.versions.filter(channel=channel).count() == 1
        self.review_version(self.version, self.url)

        v2 = self.addons['something'].versions.all()[0]
        v2.addon = self.addon
        v2.created = v2.created + timedelta(days=1)
        v2.save()
        assert self.addon.versions.filter(channel=channel).count() == 2
        action = self.review_version(v2, self.url)

        r = self.client.get(self.url)
        # The 2 following lines replace pq(res.content), it's a workaround for
        # https://github.com/gawel/pyquery/issues/31
        UTF8_PARSER = HTMLParser(encoding='utf-8')
        doc = pq(fromstring(r.content, parser=UTF8_PARSER))
        table = doc('#review-files')

        # Check the history for both versions.
        ths = table.children('tr > th')
        assert ths.length == 2
        assert '0.1' in ths.eq(0).text()
        assert '0.2' in ths.eq(1).text()

        rows = table('td.files')
        assert rows.length == 2

        comments = rows.siblings('td')
        assert comments.length == 2

        for idx in xrange(comments.length):
            td = comments.eq(idx)
            assert td.find('.history-comment').text() == 'something'
            assert td.find('th').text() == {
                'public': 'Approved',
                'info': 'More information requested'}[action]
            editor_name = td.find('td a').text()
            assert ((editor_name == self.editor.display_name) or
                    (editor_name == self.senior_editor.display_name))

    def test_item_history_with_unlisted_versions_too(self):
        # Throw in an unlisted version to be ignored.
        self.addon_file(self.addon.name, u'0.2', amo.STATUS_PUBLIC,
                        amo.STATUS_PUBLIC,
                        version_kw={'channel': amo.RELEASE_CHANNEL_UNLISTED})
        self.test_item_history()

    def test_item_history_with_unlisted_review_page(self):
        self.addon.versions.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
        self.version.reload()
        # Throw in an listed version to be ignored.
        self.addon_file(u'something', u'0.2', amo.STATUS_PUBLIC,
                        amo.STATUS_PUBLIC,
                        version_kw={'channel': amo.RELEASE_CHANNEL_LISTED})
        self.url = reverse('editors.review', args=[
            'unlisted', self.addon.slug])
        self.login_as_senior_editor()
        self.test_item_history(channel=amo.RELEASE_CHANNEL_UNLISTED)

    def generate_deleted_versions(self):
        self.addon = Addon.objects.create(type=amo.ADDON_EXTENSION,
                                          name=u'something')
        self.url = reverse('editors.review', args=[self.addon.slug])

        versions = ({'version': '0.1', 'action': 'comment',
                     'comments': 'millenium hand and shrimp'},
                    {'version': '0.1', 'action': 'public',
                     'comments': 'buggrit'},
                    {'version': '0.2', 'action': 'comment',
                     'comments': 'I told em'},
                    {'version': '0.3'})

        for i, version in enumerate(versions):
            a = create_addon_file(self.addon.name, version['version'],
                                  amo.STATUS_PUBLIC,
                                  amo.STATUS_AWAITING_REVIEW)

            v = a['version']
            v.update(created=v.created + timedelta(days=i))

            if 'action' in version:
                data = dict(action=version['action'], operating_systems='win',
                            applications='something',
                            comments=version['comments'])
                self.client.post(self.url, data)
                v.delete(hard=True)

    @patch('olympia.editors.helpers.sign_file')
    def test_item_history_deleted(self, mock_sign):
        self.generate_deleted_versions()

        r = self.client.get(self.url)
        table = pq(r.content)('#review-files')

        # Check the history for all versions.
        ths = table.children('tr > th')
        assert ths.length == 3  # The 2 with the same number will be coalesced.
        assert '0.1' in ths.eq(0).text()
        assert '0.2' in ths.eq(1).text()
        assert '0.3' in ths.eq(2).text()
        for idx in xrange(2):
            assert 'Deleted' in ths.eq(idx).text()

        bodies = table.children('.listing-body')
        assert 'millenium hand and shrimp' in bodies.eq(0).text()
        assert 'buggrit' in bodies.eq(0).text()
        assert 'I told em' in bodies.eq(1).text()

        assert mock_sign.called

    def test_item_history_compat_ordered(self):
        """ Make sure that apps in compatibility are ordered. """
        self.addon_file(u'something', u'0.2', amo.STATUS_PUBLIC,
                        amo.STATUS_AWAITING_REVIEW)

        av = AppVersion.objects.all()[0]
        v = self.addon.versions.all()[0]

        ApplicationsVersions.objects.create(
            version=v, application=amo.THUNDERBIRD.id, min=av, max=av)

        ApplicationsVersions.objects.create(
            version=v, application=amo.SEAMONKEY.id, min=av, max=av)

        assert self.addon.versions.count() == 1
        url = reverse('editors.review', args=[self.addon.slug])

        doc = pq(self.client.get(url).content)
        icons = doc('.listing-body .app-icon')
        assert icons.eq(0).attr('title') == "Firefox"
        assert icons.eq(1).attr('title') == "SeaMonkey"
        assert icons.eq(2).attr('title') == "Thunderbird"

    def test_item_history_notes(self):
        v = self.addon.versions.all()[0]
        v.releasenotes = 'hi'
        v.approvalnotes = 'secret hi'
        v.save()

        r = self.client.get(self.url)
        doc = pq(r.content)('#review-files')

        version = doc('.activity_version')
        assert version.length == 1
        assert version.text() == 'hi'

        approval = doc('.activity_approval')
        assert approval.length == 1
        assert approval.text() == 'secret hi'

    def test_item_history_header(self):
        doc = pq(self.client.get(self.url).content)
        assert ('Approved' in
                doc('#review-files .listing-header .light').text())

    def test_item_history_comment(self):
        # Add Comment.
        self.addon_file(u'something', u'0.1', amo.STATUS_PUBLIC,
                        amo.STATUS_AWAITING_REVIEW)
        self.client.post(self.url, {'action': 'comment',
                                    'comments': 'hello sailor'})

        r = self.client.get(self.url)
        doc = pq(r.content)('#review-files')
        assert doc('th').eq(1).text() == 'Comment'
        assert doc('.history-comment').text() == 'hello sailor'

    def test_files_in_item_history(self):
        data = {'action': 'public', 'operating_systems': 'win',
                'applications': 'something', 'comments': 'something'}
        self.client.post(self.url, data)

        r = self.client.get(self.url)
        items = pq(r.content)('#review-files .files .file-info')
        assert items.length == 1
        assert items.find('a.editors-install').text() == 'All Platforms'

    def test_no_items(self):
        r = self.client.get(self.url)
        assert pq(r.content)('#review-files .no-activity').length == 1

    def test_hide_beta(self):
        version = self.addon.current_version
        f = version.files.all()[0]
        version.pk = None
        version.version = '0.3beta'
        version.save()

        doc = pq(self.client.get(self.url).content)
        assert doc('#review-files tr.listing-header').length == 2

        f.pk = None
        f.status = amo.STATUS_BETA
        f.version = version
        f.save()

        doc = pq(self.client.get(self.url).content)
        assert doc('#review-files tr.listing-header').length == 1

    def test_action_links(self):
        r = self.client.get(self.url)
        expected = [
            ('View Listing', self.addon.get_url_path()),
        ]
        check_links(expected, pq(r.content)('#actions-addon a'), verify=False)

    def test_action_links_as_admin(self):
        self.login_as_admin()
        r = self.client.get(self.url)
        expected = [
            ('View Listing', self.addon.get_url_path()),
            ('Edit', self.addon.get_dev_url()),
            ('Admin Page',
             reverse('zadmin.addon_manage', args=[self.addon.id])),
        ]
        check_links(expected, pq(r.content)('#actions-addon a'), verify=False)

    def test_unlisted_addon_action_links_as_admin(self):
        """No "View Listing" link for unlisted addons, "edit"/"manage" links
        for the admins."""
        self.make_addon_unlisted(self.addon)
        self.login_as_admin()
        r = self.client.get(self.url)
        expected = [
            ('Edit', self.addon.get_dev_url()),
            ('Admin Page',
             reverse('zadmin.addon_manage', args=[self.addon.id])),
        ]
        check_links(expected, pq(r.content)('#actions-addon a'), verify=False)

    def test_admin_links_as_non_admin(self):
        self.login_as_editor()
        response = self.client.get(self.url)

        doc = pq(response.content)
        admin = doc('#actions-addon li')
        assert admin.length == 1

    def test_unflag_option_forflagged_as_admin(self):
        self.login_as_admin()
        self.addon.update(admin_review=True)
        response = self.client.get(self.url)

        doc = pq(response.content)
        assert doc('#id_adminflag').length == 1

    def test_unflag_option_forflagged_as_editor(self):
        self.login_as_editor()
        self.addon.update(admin_review=True)
        response = self.client.get(self.url)

        doc = pq(response.content)
        assert doc('#id_adminflag').length == 0

    def test_unflag_option_notflagged_as_admin(self):
        self.login_as_admin()
        self.addon.update(admin_review=False)
        response = self.client.get(self.url)

        doc = pq(response.content)
        assert doc('#id_adminflag').length == 0

    def test_unadmin_flag_as_admin(self):
        self.addon.update(admin_review=True)
        self.login_as_admin()
        response = self.client.post(self.url, {'action': 'info',
                                               'comments': 'hello sailor',
                                               'adminflag': True})
        self.assert3xx(response, reverse('editors.queue_pending'),
                       status_code=302)
        assert not Addon.objects.get(pk=self.addon.pk).admin_review

    def test_unadmin_flag_as_editor(self):
        self.addon.update(admin_review=True)
        self.login_as_editor()
        response = self.client.post(self.url, {'action': 'info',
                                               'comments': 'hello sailor',
                                               'adminflag': True})
        # Should silently fail to set adminflag but work otherwise.
        self.assert3xx(response, reverse('editors.queue_pending'),
                       status_code=302)
        assert Addon.objects.get(pk=self.addon.pk).admin_review

    def test_no_public(self):
        has_public = self.version.files.filter(
            status=amo.STATUS_PUBLIC).exists()
        assert has_public

        response = self.client.get(self.url)

        validation = pq(response.content).find('.files')
        assert validation.find('a').eq(1).text() == "Validation"
        assert validation.find('a').eq(2).text() == "Contents"

        assert validation.find('a').length == 3

    def test_public_search(self):
        self.version.files.update(status=amo.STATUS_PUBLIC)
        self.addon.update(type=amo.ADDON_SEARCH)
        r = self.client.get(self.url)
        assert pq(r.content)('#review-files .files ul .file-info').length == 1

    def test_version_deletion(self):
        """
        Make sure that we still show review history for deleted versions.
        """
        # Add a new version to the add-on.
        self.addon_file(u'something', u'0.2', amo.STATUS_PUBLIC,
                        amo.STATUS_AWAITING_REVIEW)

        assert self.addon.versions.count() == 1

        self.review_version(self.version, self.url)

        v2 = self.addons['something'].versions.all()[0]
        v2.addon = self.addon
        v2.created = v2.created + timedelta(days=1)
        v2.save()
        self.review_version(v2, self.url)
        assert self.addon.versions.count() == 2

        r = self.client.get(self.url)
        doc = pq(r.content)

        # View the history verify two versions:
        ths = doc('table#review-files > tr > th:first-child')
        assert '0.1' in ths.eq(0).text()
        assert '0.2' in ths.eq(1).text()

        # Delete a version:
        v2.delete()
        # Verify two versions, one deleted:
        r = self.client.get(self.url)
        doc = pq(r.content)
        ths = doc('table#review-files > tr > th:first-child')

        assert ths.length == 2
        assert '0.1' in ths.text()

    def test_no_versions(self):
        """The review page should still load if there are no versions. But not
        unless you have unlisted permissions."""
        assert self.client.get(self.url).status_code == 200
        response = self.client.post(self.url, {'action': 'info',
                                               'comments': 'hello sailor'})
        assert response.status_code == 302
        self.assert3xx(response, reverse('editors.queue_pending'),
                       status_code=302)

        self.version.delete()
        # Regular reviewer has no permission, gets a 404.
        assert self.client.get(self.url).status_code == 404
        self.login_as_senior_editor()
        # Reviewer with more powers can look.
        assert self.client.get(self.url).status_code == 200
        response = self.client.post(self.url, {'action': 'info',
                                               'comments': 'hello sailor'})
        assert response.status_code == 302
        self.assert3xx(response, reverse('editors.queue_pending'),
                       status_code=302)

    def test_addon_deleted(self):
        """The review page should still load for deleted addons."""
        self.addon.delete()
        self.url = reverse('editors.review', args=[self.addon.pk])

        assert self.client.get(self.url).status_code == 200
        response = self.client.post(self.url, {'action': 'info',
                                               'comments': 'hello sailor'})
        assert response.status_code == 302
        self.assert3xx(response, reverse('editors.queue_pending'),
                       status_code=302)

    @patch('olympia.editors.helpers.sign_file')
    def review_version(self, version, url, mock_sign):
        if version.channel == amo.RELEASE_CHANNEL_LISTED:
            version.files.all()[0].update(status=amo.STATUS_AWAITING_REVIEW)
            action = 'public'
        else:
            action = 'info'

        data = dict(action=action, operating_systems='win',
                    applications='something', comments='something')
        self.client.post(url, data)

        if version.channel == amo.RELEASE_CHANNEL_LISTED:
            assert mock_sign.called
        return action

    def test_dependencies_listed(self):
        AddonDependency.objects.create(addon=self.addon,
                                       dependent_addon=self.addon)
        r = self.client.get(self.url)
        deps = pq(r.content)('#addon-summary .addon-dependencies')
        assert deps.length == 1
        assert deps.find('li').length == 1
        assert deps.find('a').attr('href') == self.addon.get_url_path()

    def test_eula_displayed(self):
        assert not bool(self.addon.eula)
        r = self.client.get(self.url)
        assert r.status_code == 200
        self.assertNotContains(r, 'View End-User License Agreement')

        self.addon.eula = 'Test!'
        self.addon.save()
        assert bool(self.addon.eula)
        r = self.client.get(self.url)
        assert r.status_code == 200
        self.assertContains(r, 'View End-User License Agreement')

    def test_privacy_policy_displayed(self):
        assert self.addon.privacy_policy is None
        r = self.client.get(self.url)
        assert r.status_code == 200
        self.assertNotContains(r, 'View Privacy Policy')

        self.addon.privacy_policy = 'Test!'
        self.addon.save()
        r = self.client.get(self.url)
        assert r.status_code == 200
        self.assertContains(r, 'View Privacy Policy')

    def test_breadcrumbs_all(self):
        queues = {'New Add-ons': amo.STATUS_NOMINATED,
                  'Updates': amo.STATUS_PUBLIC}
        for text, queue_id in queues.items():
            self.addon.update(status=queue_id)
            doc = pq(self.client.get(self.url).content)
            assert doc('#breadcrumbs li').eq(1).text() == text

    def test_viewing(self):
        url = reverse('editors.review_viewing')
        r = self.client.post(url, {'addon_id': self.addon.id})
        data = json.loads(r.content)
        assert data['current'] == self.editor.id
        assert data['current_name'] == self.editor.name
        assert data['is_user'] == 1

        # Now, login as someone else and test.
        self.login_as_admin()
        r = self.client.post(url, {'addon_id': self.addon.id})
        data = json.loads(r.content)
        assert data['current'] == self.editor.id
        assert data['current_name'] == self.editor.name
        assert data['is_user'] == 0

    # Lets just override this to make the test a bit shorter.
    @mock.patch.object(amo, 'EDITOR_REVIEW_LOCK_LIMIT', 1)
    def test_viewing_lock_limit(self):
        url = reverse('editors.review_viewing')

        res = self.client.post(url, {'addon_id': 1234})
        data = json.loads(res.content)
        assert data['current'] == self.editor.id
        assert data['current_name'] == self.editor.name
        assert data['is_user'] == 1

        # Second review page is over the limit.
        res = self.client.post(url, {'addon_id': 5678})
        data = json.loads(res.content)
        assert data['current'] == settings.TASK_USER_ID  # Mozilla's task ID.
        assert data['current_name'] == 'Review lock limit reached'
        assert data['is_user'] == 2

        # Now, login as someone else and test.  First page is blocked.
        self.login_as_admin()
        res = self.client.post(url, {'addon_id': 1234})
        data = json.loads(res.content)
        assert data['current'] == self.editor.id
        assert data['current_name'] == self.editor.name
        assert data['is_user'] == 0

        # Second page is available.
        res = self.client.post(url, {'addon_id': 5678})
        data = json.loads(res.content)
        admin = UserProfile.objects.get(username='admin')
        assert data['current'] == admin.id
        assert data['current_name'] == admin.name
        assert data['is_user'] == 1

    # Lets just override this to make the test a bit shorter.
    @mock.patch.object(amo, 'EDITOR_REVIEW_LOCK_LIMIT', 1)
    def test_viewing_lock_admin(self):
        self.login_as_admin()
        url = reverse('editors.review_viewing')
        admin = UserProfile.objects.get(username='admin')

        res = self.client.post(url, {'addon_id': 101})
        data = json.loads(res.content)
        assert data['current'] == admin.id
        assert data['current_name'] == admin.name
        assert data['is_user'] == 1

        # Admin don't have time for no limits.
        res = self.client.post(url, {'addon_id': 202})
        data = json.loads(res.content)
        assert data['current'] == admin.id
        assert data['current_name'] == admin.name
        assert data['is_user'] == 1

    def test_viewing_review_unlocks(self):
        reviewing_url = reverse('editors.review_viewing')
        self.client.post(reviewing_url, {'addon_id': self.addon.id})
        key = '%s:review_viewing:%s' % (settings.CACHE_PREFIX, self.addon.id)
        assert cache.get(key) == self.editor.id

        self.client.post(self.url, {'action': 'comment',
                                    'comments': 'hello sailor'})
        # Processing a review should instantly clear the review lock on it.
        assert cache.get(key) is None

    def test_viewing_queue(self):
        r = self.client.post(reverse('editors.review_viewing'),
                             {'addon_id': self.addon.id})
        data = json.loads(r.content)
        assert data['current'] == self.editor.id
        assert data['current_name'] == self.editor.name
        assert data['is_user'] == 1

        # Now, login as someone else and test.
        self.login_as_admin()
        r = self.client.post(reverse('editors.queue_viewing'),
                             {'addon_ids': self.addon.id})
        data = json.loads(r.content)
        assert data[str(self.addon.id)] == self.editor.display_name

    def test_display_same_files_only_once(self):
        """
        Test whether identical files for different platforms
        show up as one link with the appropriate text.
        """
        version = Version.objects.create(addon=self.addon, version='0.2')
        version.created = datetime.today() + timedelta(days=1)
        version.save()

        for plat in (amo.PLATFORM_WIN, amo.PLATFORM_MAC):
            File.objects.create(platform=plat.id, version=version,
                                status=amo.STATUS_PUBLIC)
        self.addon.update(_current_version=version)

        r = self.client.get(self.url)
        text = pq(r.content)('.editors-install').eq(1).text()
        assert text == "Windows / Mac OS X"

    def test_no_compare_link(self):
        r = self.client.get(self.url)
        assert r.status_code == 200
        info = pq(r.content)('#review-files .file-info')
        assert info.length == 1
        assert info.find('a.compare').length == 0

    def test_compare_link(self):
        version = Version.objects.create(addon=self.addon, version='0.2')
        version.created = datetime.today() + timedelta(days=1)
        version.save()

        f1 = self.addon.versions.order_by('created')[0].files.all()[0]
        f1.status = amo.STATUS_PUBLIC
        f1.save()

        f2 = File.objects.create(version=version, status=amo.STATUS_PUBLIC)
        self.addon.update(_current_version=version)
        assert self.addon.current_version == version

        r = self.client.get(self.url)
        assert r.context['show_diff']
        links = pq(r.content)('#review-files .file-info .compare')
        expected = [
            reverse('files.compare', args=[f2.pk, f1.pk]),
        ]
        check_links(expected, links, verify=False)

    def test_download_sources_link(self):
        version = self.addon.current_version
        tdir = temp.gettempdir()
        source_file = temp.NamedTemporaryFile(suffix='.zip', dir=tdir)
        source_file.write('a' * (2 ** 21))
        source_file.seek(0)
        version.source = DjangoFile(source_file)
        version.save()

        url = reverse('editors.review', args=[self.addon.pk])

        # Admin reviewer: able to download sources.
        user = UserProfile.objects.get(email='admin@mozilla.com')
        self.client.login(email=user.email)
        response = self.client.get(url, follow=True)
        assert 'Download files' in response.content

        # Standard reviewer: should know that sources were provided.
        user = UserProfile.objects.get(email='editor@mozilla.com')
        self.client.login(email=user.email)
        response = self.client.get(url, follow=True)
        assert 'The developer has provided source code.' in response.content

    @patch('olympia.editors.helpers.sign_file')
    def test_admin_flagged_addon_actions_as_admin(self, mock_sign_file):
        self.version.files.update(status=amo.STATUS_AWAITING_REVIEW)
        self.addon.update(admin_review=True, status=amo.STATUS_NOMINATED)
        self.login_as_admin()
        response = self.client.post(self.url, self.get_dict(action='public'),
                                    follow=True)
        assert response.status_code == 200
        addon = self.get_addon()
        assert self.version == addon.current_version
        assert addon.status == amo.STATUS_PUBLIC
        assert addon.current_version.files.all()[0].status == amo.STATUS_PUBLIC

        assert mock_sign_file.called

    def test_admin_flagged_addon_actions_as_editor(self):
        self.version.files.update(status=amo.STATUS_AWAITING_REVIEW)
        self.addon.update(admin_review=True, status=amo.STATUS_NOMINATED)
        self.login_as_editor()
        response = self.client.post(self.url, self.get_dict(action='public'))
        assert response.status_code == 200  # Form error.
        # The add-on status must not change as non-admin editors are not
        # allowed to review admin-flagged add-ons.
        addon = self.get_addon()
        assert addon.status == amo.STATUS_NOMINATED
        assert self.version == addon.current_version
        assert addon.current_version.files.all()[0].status == (
            amo.STATUS_AWAITING_REVIEW)
        assert response.context['form'].errors['action'] == (
            [u'Select a valid choice. public is not one of the available '
             u'choices.'])

    def test_user_changes_log(self):
        # Activity logs related to user changes should be displayed.
        # Create an activy log for each of the following: user addition, role
        # change and deletion.
        author = self.addon.addonuser_set.get()
        from olympia.amo import set_user
        set_user(author.user)
        amo.log(amo.LOG.ADD_USER_WITH_ROLE,
                author.user, author.get_role_display(), self.addon)
        amo.log(amo.LOG.CHANGE_USER_WITH_ROLE,
                author.user, author.get_role_display(), self.addon)
        amo.log(amo.LOG.REMOVE_USER_WITH_ROLE,
                author.user, author.get_role_display(), self.addon)

        response = self.client.get(self.url)
        assert 'user_changes' in response.context
        user_changes_log = response.context['user_changes']
        actions = [log.activity_log.action for log in user_changes_log]
        assert actions == [
            amo.LOG.ADD_USER_WITH_ROLE.id,
            amo.LOG.CHANGE_USER_WITH_ROLE.id,
            amo.LOG.REMOVE_USER_WITH_ROLE.id]

        # Make sure the logs are displayed in the page.
        doc = pq(response.content)
        user_changes = doc('#user-changes li')
        assert len(user_changes) == 3
        assert '(Owner) added to ' in user_changes[0].text
        assert 'role changed to Owner for ' in user_changes[1].text
        assert '(Owner) removed from ' in user_changes[2].text

    @override_settings(CELERY_ALWAYS_EAGER=True)
    @mock.patch('olympia.devhub.tasks.validate')
    def test_validation_not_run_eagerly(self, validate):
        """Tests that validation is not run in eager mode."""
        assert not self.file.has_been_validated

        self.client.get(self.url)

        assert not validate.called

    @override_settings(CELERY_ALWAYS_EAGER=False)
    @mock.patch('olympia.devhub.tasks.validate')
    def test_validation_run(self, validate):
        """Tests that validation is run if necessary."""
        assert not self.file.has_been_validated

        self.client.get(self.url)

        validate.assert_called_once_with(self.file)

    @override_settings(CELERY_ALWAYS_EAGER=False)
    @mock.patch('olympia.devhub.tasks.validate')
    def test_validation_not_run_again(self, validate):
        """Tests that validation is not run for files which have cached
        results."""

        FileValidation.objects.create(file=self.file, validation=json.dumps(
            amo.VALIDATOR_SKELETON_RESULTS))

        self.client.get(self.url)

        assert not validate.called

    def test_review_is_review_listed(self):
        review_page = self.client.get(
            reverse('editors.review', args=[self.addon.slug]))
        listed_review_page = self.client.get(
            reverse('editors.review', args=['listed', self.addon.slug]))
        assert (pq(review_page.content)('#review-files').text() ==
                pq(listed_review_page.content)('#review-files').text())


class TestReviewPending(ReviewBase):

    def setUp(self):
        super(TestReviewPending, self).setUp()
        self.file = File.objects.create(version=self.version,
                                        status=amo.STATUS_AWAITING_REVIEW)
        self.addon.update(status=amo.STATUS_PUBLIC)

    def pending_dict(self):
        return self.get_dict(action='public')

    @patch('olympia.editors.helpers.sign_file')
    def test_pending_to_public(self, mock_sign):
        statuses = (self.version.files.values_list('status', flat=True)
                    .order_by('status'))
        assert list(statuses) == [
            amo.STATUS_AWAITING_REVIEW, amo.STATUS_PUBLIC]

        response = self.client.post(self.url, self.pending_dict())
        assert self.get_addon().status == amo.STATUS_PUBLIC
        self.assert3xx(response, reverse('editors.queue_pending'))

        statuses = (self.version.files.values_list('status', flat=True)
                    .order_by('status'))
        assert list(statuses) == [amo.STATUS_PUBLIC, amo.STATUS_PUBLIC]

        assert mock_sign.called

    def test_display_only_unreviewed_files(self):
        """Only the currently unreviewed files are displayed."""
        self.file.update(filename='somefilename.xpi')
        reviewed = File.objects.create(version=self.version,
                                       status=amo.STATUS_PUBLIC,
                                       filename='file_reviewed.xpi')
        disabled = File.objects.create(version=self.version,
                                       status=amo.STATUS_DISABLED,
                                       filename='file_disabled.xpi')
        unreviewed = File.objects.create(version=self.version,
                                         status=amo.STATUS_AWAITING_REVIEW,
                                         filename='file_unreviewed.xpi')
        response = self.client.get(self.url, self.pending_dict())
        doc = pq(response.content)
        assert len(doc('.review-actions-files ul li')) == 2
        assert reviewed.filename not in response.content
        assert disabled.filename not in response.content
        assert unreviewed.filename in response.content
        assert self.file.filename in response.content

    @patch('olympia.editors.helpers.sign_file')
    def test_review_unreviewed_files(self, mock_sign):
        """Review all the unreviewed files when submitting a review."""
        reviewed = File.objects.create(version=self.version,
                                       status=amo.STATUS_PUBLIC)
        disabled = File.objects.create(version=self.version,
                                       status=amo.STATUS_DISABLED)
        unreviewed = File.objects.create(version=self.version,
                                         status=amo.STATUS_AWAITING_REVIEW)
        self.login_as_admin()
        response = self.client.post(self.url, self.pending_dict())
        self.assert3xx(response, reverse('editors.queue_pending'))

        assert self.addon.reload().status == amo.STATUS_PUBLIC
        assert reviewed.reload().status == amo.STATUS_PUBLIC
        assert disabled.reload().status == amo.STATUS_DISABLED
        assert unreviewed.reload().status == amo.STATUS_PUBLIC
        assert self.file.reload().status == amo.STATUS_PUBLIC

        assert mock_sign.called


class TestEditorMOTD(EditorTest):

    def get_url(self, save=False):
        return reverse('editors.%smotd' % ('save_' if save else ''))

    def test_change_motd(self):
        self.login_as_admin()
        motd = "Let's get crazy"
        r = self.client.post(self.get_url(save=True), {'motd': motd})
        url = self.get_url()
        self.assert3xx(r, url)
        r = self.client.get(url)
        assert pq(r.content)('.daily-message p').text() == motd

    def test_require_editor_to_view(self):
        url = self.get_url()
        self.assertLoginRedirects(self.client.head(url), to=url)

    def test_require_admin_to_change_motd(self):
        self.login_as_editor()
        r = self.client.post(reverse('editors.save_motd'),
                             {'motd': "I'm a sneaky editor"})
        assert r.status_code == 403

    def test_editor_can_view_not_edit(self):
        motd = 'Some announcement'
        set_config('editors_review_motd', motd)
        self.login_as_editor()
        r = self.client.get(self.get_url())
        assert pq(r.content)('.daily-message p').text() == motd
        assert r.context['form'] is None

    def test_motd_edit_group(self):
        user = UserProfile.objects.get(email='editor@mozilla.com')
        group = Group.objects.create(name='Add-on Reviewer MOTD',
                                     rules='AddonReviewerMOTD:Edit')
        GroupUser.objects.create(user=user, group=group)
        self.login_as_editor()
        r = self.client.post(reverse('editors.save_motd'),
                             {'motd': 'I am the keymaster.'})
        assert r.status_code == 302
        assert get_config('editors_review_motd') == 'I am the keymaster.'

    def test_form_errors(self):
        self.login_as_admin()
        r = self.client.post(self.get_url(save=True))
        doc = pq(r.content)
        assert doc('#editor-motd .errorlist').text() == (
            'This field is required.')

    def test_motd_tab(self):
        self.login_as_admin()
        r = self.client.get(self.get_url())
        announcement_tab = pq(r.content)(
            'li.top:nth-child(5) > a:nth-child(1)').text()
        assert announcement_tab == 'Announcement'


class TestStatusFile(ReviewBase):

    def get_file(self):
        return self.version.files.all()[0]

    def check_status(self, expected):
        r = self.client.get(self.url)
        assert pq(r.content)('#review-files .file-info div').text() == expected

    def test_status_full(self):
        self.get_file().update(status=amo.STATUS_AWAITING_REVIEW)
        for status in [amo.STATUS_NOMINATED, amo.STATUS_PUBLIC]:
            self.addon.update(status=status)
            self.check_status('Awaiting Review')

    def test_status_full_reviewed(self):
        self.get_file().update(status=amo.STATUS_PUBLIC)
        self.addon.update(status=amo.STATUS_PUBLIC)
        self.check_status('Approved')

    def test_other(self):
        self.addon.update(status=amo.STATUS_BETA)
        self.check_status(unicode(File.STATUS_CHOICES[self.get_file().status]))


class TestWhiteboard(ReviewBase):

    def test_whiteboard_addition(self):
        whiteboard_info = u'Whiteboard info.'
        url = reverse('editors.whiteboard', args=[
            self.addon.slug if not self.addon.is_deleted else self.addon.pk])
        response = self.client.post(url, {'whiteboard': whiteboard_info})
        assert response.status_code == 302
        assert self.addon.reload().whiteboard == whiteboard_info

    @patch('olympia.addons.decorators.owner_or_unlisted_reviewer',
           lambda r, a: True)
    def test_whiteboard_addition_unlisted_addon(self):
        self.make_addon_unlisted(self.addon)
        whiteboard_info = u'Whiteboard info.'
        url = reverse('editors.whiteboard', args=[
            self.addon.slug if not self.addon.is_deleted else self.addon.pk])
        response = self.client.post(url, {'whiteboard': whiteboard_info})
        assert response.status_code == 302
        assert self.addon.reload().whiteboard == whiteboard_info


class TestWhiteboardDeleted(TestWhiteboard):

    def setUp(self):
        super(TestWhiteboardDeleted, self).setUp()
        self.addon.delete()


class TestAbuseReports(TestCase):
    fixtures = ['base/users', 'base/addon_3615']

    def setUp(self):
        user = UserProfile.objects.all()[0]
        AbuseReport.objects.create(addon_id=3615, message='woo')
        AbuseReport.objects.create(addon_id=3615, message='yeah',
                                   reporter=user)
        # Make a user abuse report to make sure it doesn't show up.
        AbuseReport.objects.create(user=user, message='hey now')

    def test_abuse_reports_list(self):
        assert self.client.login(email='admin@mozilla.com')
        r = self.client.get(reverse('editors.abuse_reports', args=['a3615']))
        assert r.status_code == 200
        # We see the two abuse reports created in setUp.
        assert len(r.context['reports']) == 2

    def test_no_abuse_reports_link_for_unlisted_addons(self):
        """Unlisted addons aren't public, and thus have no abuse reports."""
        addon = Addon.objects.get(pk=3615)
        self.make_addon_unlisted(addon)
        self.client.login(email='admin@mozilla.com')
        response = reverse('editors.review', args=[addon.slug])
        abuse_report_url = reverse('editors.abuse_reports', args=['a3615'])
        assert abuse_report_url not in response


class TestLeaderboard(EditorTest):
    fixtures = ['base/users']

    def setUp(self):
        super(TestLeaderboard, self).setUp()
        self.url = reverse('editors.leaderboard')

        self.user = UserProfile.objects.get(email='editor@mozilla.com')
        self.login_as_editor()
        amo.set_user(self.user)

    def _award_points(self, user, score):
        ReviewerScore.objects.create(user=user, note_key=amo.REVIEWED_MANUAL,
                                     score=score, note='Thing.')

    def test_leaderboard_ranks(self):
        users = (self.user,
                 UserProfile.objects.get(email='regular@mozilla.com'),
                 UserProfile.objects.get(email='clouserw@gmail.com'))

        self._award_points(users[0], amo.REVIEWED_LEVELS[0]['points'] - 1)
        self._award_points(users[1], amo.REVIEWED_LEVELS[0]['points'] + 1)
        self._award_points(users[2], amo.REVIEWED_LEVELS[0]['points'] + 2)

        def get_cells():
            doc = pq(self.client.get(self.url).content.decode('utf-8'))

            cells = doc('#leaderboard > tbody > tr > .name, '
                        '#leaderboard > tbody > tr > .level')

            return [cells.eq(i).text() for i in range(0, cells.length)]

        assert get_cells() == (
            [users[2].display_name,
             users[1].display_name,
             amo.REVIEWED_LEVELS[0]['name'],
             users[0].display_name])

        self._award_points(users[0], 1)

        assert get_cells() == (
            [users[2].display_name,
             users[1].display_name,
             users[0].display_name,
             amo.REVIEWED_LEVELS[0]['name']])

        self._award_points(users[0], -1)
        self._award_points(users[2], (amo.REVIEWED_LEVELS[1]['points'] -
                                      amo.REVIEWED_LEVELS[0]['points']))

        assert get_cells() == (
            [users[2].display_name,
             amo.REVIEWED_LEVELS[1]['name'],
             users[1].display_name,
             amo.REVIEWED_LEVELS[0]['name'],
             users[0].display_name])


class TestXssOnAddonName(amo.tests.TestXss):

    def test_editors_abuse_report_page(self):
        url = reverse('editors.abuse_reports', args=[self.addon.slug])
        self.assertNameAndNoXSS(url)

    def test_editors_review_page(self):
        url = reverse('editors.review', args=[self.addon.slug])
        self.assertNameAndNoXSS(url)


class LimitedReviewerBase:
    def create_limited_user(self):
        limited_user = UserProfile.objects.create(username='limited',
                                                  email="limited@mozilla.com")
        limited_user.save()

        permissions = [
            {
                'name': 'Add-on Reviewers',
                'rules': 'Addons:Review',
            },
            {
                'name': 'Limited Reviewers',
                'rules': 'Addons:DelayedReviews',
            },
        ]
        for perm in permissions:
            group = Group.objects.create(name=perm['name'],
                                         rules=perm['rules'])
            GroupUser.objects.create(group=group, user=limited_user)

    def login_as_limited_reviewer(self):
        self.client.logout()
        assert self.client.login(email='limited@mozilla.com')


class TestLimitedReviewerQueue(QueueTest, LimitedReviewerBase):

    def setUp(self):
        super(TestLimitedReviewerQueue, self).setUp()
        self.url = reverse('editors.queue_nominated')

        for addon in self.generate_files().values():
            version = addon.find_latest_version(
                channel=amo.RELEASE_CHANNEL_LISTED)
            if version.nomination <= datetime.now() - timedelta(
                    hours=REVIEW_LIMITED_DELAY_HOURS):
                self.expected_addons.append(addon)

        self.create_limited_user()
        self.login_as_limited_reviewer()

    def generate_files(self, subset=None):
        if subset is None:
            subset = []
        files = SortedDict([
            ('Nominated new', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
                'nomination': datetime.now()
            }),
            ('Nominated old', {
                'version_str': '0.1',
                'addon_status': amo.STATUS_NOMINATED,
                'file_status': amo.STATUS_AWAITING_REVIEW,
                'nomination': datetime.now() - timedelta(days=1)
            }),
        ])
        results = {}
        for name, attrs in files.iteritems():
            if not subset or name in subset:
                results[name] = self.addon_file(name, **attrs)
        return results

    def test_results(self):
        self._test_results()

    def test_queue_count(self):
        self._test_queue_count(0, 'New Add-on', 1)

    def test_get_queue(self):
        self._test_get_queue()


class TestLimitedReviewerReview(ReviewBase, LimitedReviewerBase):

    def setUp(self):
        super(TestLimitedReviewerReview, self).setUp()

        self.create_limited_user()
        self.login_as_limited_reviewer()

    def test_new_addon_review_action_as_limited_editor(self):
        self.addon.update(status=amo.STATUS_NOMINATED)
        self.version.update(nomination=datetime.now())
        self.version.files.update(status=amo.STATUS_AWAITING_REVIEW)
        response = self.client.post(self.url, self.get_dict(action='public'))
        assert response.status_code == 200  # Form error.
        # The add-on status must not change as limited reviewers are not
        # allowed to review recently submitted add-ons.
        assert self.get_addon().status == amo.STATUS_NOMINATED
        assert response.context['form'].errors['action'] == [
            u'Select a valid choice. public is not one of the available '
            u'choices.']

    @patch('olympia.editors.helpers.sign_file')
    def test_old_addon_review_action_as_limited_editor(self, mock_sign_file):
        self.version.files.update(status=amo.STATUS_AWAITING_REVIEW)
        self.version.update(nomination=datetime.now() - timedelta(days=1))
        self.addon.update(status=amo.STATUS_NOMINATED)
        response = self.client.post(self.url, self.get_dict(action='public'),
                                    follow=True)
        assert response.status_code == 200
        assert self.get_addon().status == amo.STATUS_PUBLIC
        assert mock_sign_file.called

    def test_limited_editor_no_version(self):
        version = self.addon.find_latest_version(
            channel=amo.RELEASE_CHANNEL_LISTED)
        version.delete()
        response = self.client.get(self.url)
        assert response.status_code == 404
