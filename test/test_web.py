# -*- coding: utf8 -*-
# This file is part of PyBossa.
#
# Copyright (C) 2013 SF Isle of Man Limited
#
# PyBossa is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PyBossa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with PyBossa.  If not, see <http://www.gnu.org/licenses/>.

import json
import StringIO

from default import db, Fixtures, with_context
from helper import web
from mock import patch, Mock
from flask import Response, redirect
from itsdangerous import BadSignature
from collections import namedtuple
from pybossa.core import signer
from pybossa.util import unicode_csv_reader
from pybossa.util import get_user_signup_method
from pybossa.ckan import Ckan
from bs4 import BeautifulSoup
from requests.exceptions import ConnectionError
from werkzeug.exceptions import NotFound
from pybossa.model.app import App
from pybossa.model.category import Category
from pybossa.model.task import Task
from pybossa.model.task_run import TaskRun
from pybossa.model.user import User
from pybossa.jobs import send_mail, import_tasks
from factories import AppFactory, CategoryFactory, TaskFactory, TaskRunFactory


FakeRequest = namedtuple('FakeRequest', ['text', 'status_code', 'headers'])


class TestWeb(web.Helper):
    pkg_json_not_found = {
        "help": "Return ...",
        "success": False,
        "error": {
            "message": "Not found",
            "__type": "Not Found Error"}}

    @with_context
    def test_01_index(self):
        """Test WEB home page works"""
        res = self.app.get("/", follow_redirects=True)
        assert self.html_title() in res.data, res
        assert "Create a Project" in res.data, res

    @with_context
    def test_01_search(self):
        """Test WEB search page works."""
        res = self.app.get('/search')
        err_msg = "Search page should be accessible"
        assert "Search" in res.data, err_msg

    @with_context
    @patch('pybossa.cache.project_stats.pygeoip', autospec=True)
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_02_stats(self, mock1, mock2):
        """Test WEB leaderboard or stats page works"""
        res = self.register()
        res = self.signin()
        res = self.new_application(short_name="igil")
        returns = [Mock()]
        returns[0].GeoIP.return_value = 'gic'
        returns[0].GeoIP.record_by_addr.return_value = {}
        mock1.side_effects = returns

        app = db.session.query(App).first()
        user = db.session.query(User).first()
        # Without stats
        url = '/app/%s/stats' % app.short_name
        res = self.app.get(url)
        assert "Sorry" in res.data, res.data

        # We use a string here to check that it works too
        task = Task(app_id=app.id, n_answers=10)
        db.session.add(task)
        db.session.commit()

        for i in range(10):
            task_run = TaskRun(app_id=app.id, task_id=1,
                                     user_id=user.id,
                                     info={'answer': 1})
            db.session.add(task_run)
            db.session.commit()
            self.app.get('api/app/%s/newtask' % app.id)

        # With stats
        url = '/app/%s/stats' % app.short_name
        res = self.app.get(url)
        assert res.status_code == 200, res.status_code
        assert "Distribution" in res.data, res.data

        with patch.dict(self.flask_app.config, {'GEO': True}):
            url = '/app/%s/stats' % app.short_name
            res = self.app.get(url)
            assert "GeoLite" in res.data, res.data

        res = self.app.get('/leaderboard', follow_redirects=True)
        assert self.html_title("Community Leaderboard") in res.data, res
        assert user.name in res.data, res.data

        # With hidden project
        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        url = '/app/%s/stats' % app.short_name
        res = self.app.get(url)
        assert res.status_code == 200, res.status_code
        assert "Distribution" in res.data, res.data
        self.signout()

        self.create()
        # As anonymous
        url = '/app/%s/stats' % app.short_name
        res = self.app.get(url)
        assert res.status_code == 401, res.status_code
        # As another user, but not owner
        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        url = '/app/%s/stats' % app.short_name
        res = self.app.get(url)
        assert res.status_code == 403, res.status_code

    @with_context
    def test_03_account_index(self):
        """Test WEB account index works."""
        # Without users
        res = self.app.get('/account/page/15', follow_redirects=True)
        assert res.status_code == 404, res.status_code

        self.create()
        res = self.app.get('/account', follow_redirects=True)
        assert res.status_code == 200, res.status_code
        err_msg = "There should be a Community page"
        assert "Community" in res.data, err_msg


    @with_context
    def test_register_get(self):
        """Test WEB register user works"""
        res = self.app.get('/account/register')
        # The output should have a mime-type: text/html
        assert res.mimetype == 'text/html', res
        assert self.html_title("Register") in res.data, res


    @with_context
    @patch('pybossa.view.account.mail_queue', autospec=True)
    @patch('pybossa.view.account.render_template')
    @patch('pybossa.view.account.signer')
    def test_register_post_creates_email_with_link(self, signer, render, queue):
        """Test WEB register post creates and sends the confirmation email if
        account validation is enabled"""
        from flask import current_app
        current_app.config['ACCOUNT_CONFIRMATION_DISABLED'] = False
        data = dict(fullname="John Doe", name="johndoe",
                    password="p4ssw0rd", confirm="p4ssw0rd",
                    email_addr="johndoe@example.com")
        signer.dumps.return_value = ''
        render.return_value = ''
        res = self.app.post('/account/register', data=data)
        del data['confirm']
        current_app.config['ACCOUNT_CONFIRMATION_DISABLED'] = True

        signer.dumps.assert_called_with(data, salt='account-validation')
        render.assert_any_call('/account/email/validate_account.md',
                                user=data,
                                confirm_url='http://localhost/account/register/confirmation?key=')
        assert send_mail == queue.enqueue.call_args[0][0], "send_mail not called"
        mail_data = queue.enqueue.call_args[0][1]
        assert 'subject' in mail_data.keys()
        assert 'recipients' in mail_data.keys()
        assert 'body' in mail_data.keys()
        assert 'html' in mail_data.keys()


    @with_context
    def test_register_post_valid_data_validation_enabled(self):
        """Test WEB register post with valid form data and account validation
        enabled"""
        from flask import current_app
        current_app.config['ACCOUNT_CONFIRMATION_DISABLED'] = False
        data = dict(fullname="John Doe", name="johndoe",
                    password="p4ssw0rd", confirm="p4ssw0rd",
                    email_addr="johndoe@example.com")

        res = self.app.post('/account/register', data=data)
        current_app.config['ACCOUNT_CONFIRMATION_DISABLED'] = True
        assert self.html_title() in res.data, res
        assert "Just one more step, please" in res.data, res.data


    @with_context
    @patch('pybossa.view.account.redirect', wraps=redirect)
    @patch('pybossa.view.account.signer')
    def test_register_post_valid_data_validation_disabled(self, signer, redirect):
        """Test WEB register post with valid form data and account validation
        disabled redirects to the confirmation URL with valid arguments"""
        data = dict(fullname="John Doe", name="johndoe",
                    password="p4ssw0rd", confirm="p4ssw0rd",
                    email_addr="johndoe@example.com")
        signer.dumps.return_value = 'key'
        res = self.app.post('/account/register', data=data)
        print dir(redirect)
        redirect.assert_called_with('http://localhost/account/register/confirmation?key=key')


    def test_register_confirmation_fails_without_key(self):
        """Test WEB register confirmation returns 403 if no 'key' param is present"""
        res = self.app.get('/account/register/confirmation')

        assert res.status_code == 403, res.status


    def test_register_confirmation_fails_with_invalid_key(self):
        """Test WEB register confirmation returns 403 if an invalid key is given"""
        res = self.app.get('/account/register/confirmation?key=invalid')

        assert res.status_code == 403, res.status


    @patch('pybossa.view.account.signer')
    def test_register_confirmation_gets_account_data_from_key(self, fake_signer):
        """Test WEB register confirmation gets the account data from the key"""
        fake_signer.loads.return_value = dict(fullname='FN', name='name',
                       email_addr='email', password='password')
        res = self.app.get('/account/register/confirmation?key=valid-key')

        fake_signer.loads.assert_called_with('valid-key', max_age=3600, salt='account-validation')


    @patch('pybossa.view.account.signer')
    def test_register_confirmation_creates_new_account(self, fake_signer):
        """Test WEB register confirmation creates the new account"""
        fake_signer.loads.return_value = dict(fullname='FN', name='name',
                       email_addr='email', password='password')
        res = self.app.get('/account/register/confirmation?key=valid-key')

        user = db.session.query(User).filter_by(name='name').first()

        assert user is not None
        assert user.check_password('password')


    @with_context
    def test_04_signin_signout(self):
        """Test WEB sign in and sign out works"""
        res = self.register()
        # Log out as the registration already logs in the user
        res = self.signout()

        res = self.signin(method="GET")
        assert self.html_title("Sign in") in res.data, res.data
        assert "Sign in" in res.data, res.data

        res = self.signin(email='')
        assert "Please correct the errors" in res.data, res
        assert "The e-mail is required" in res.data, res

        res = self.signin(password='')
        assert "Please correct the errors" in res.data, res
        assert "You must provide a password" in res.data, res

        res = self.signin(email='', password='')
        assert "Please correct the errors" in res.data, res
        assert "The e-mail is required" in res.data, res
        assert "You must provide a password" in res.data, res

        # Non-existant user
        msg = "Ooops, we didn't find you in the system"
        res = self.signin(email='wrongemail')
        assert msg in res.data, res.data

        res = self.signin(email='wrongemail', password='wrongpassword')
        assert msg in res.data, res

        # Real user but wrong password or username
        msg = "Ooops, Incorrect email/password"
        res = self.signin(password='wrongpassword')
        assert msg in res.data, res

        res = self.signin()
        assert self.html_title() in res.data, res
        assert "Welcome back %s" % "John Doe" in res.data, res

        # Check profile page with several information chunks
        res = self.profile()
        assert self.html_title("Profile") in res.data, res
        assert "John Doe" in res.data, res
        assert "johndoe@example.com" in res.data, res

        # Log out
        res = self.signout()
        assert self.html_title() in res.data, res
        assert "You are now signed out" in res.data, res

        # Request profile as an anonymous user
        # Check profile page with several information chunks
        res = self.profile()
        assert "John Doe" in res.data, res
        assert "johndoe@example.com" not in res.data, res

        # Try to access protected areas like update
        res = self.app.get('/account/johndoe/update', follow_redirects=True)
        # As a user must be signed in to access, the page the title will be the
        # redirection to log in
        assert self.html_title("Sign in") in res.data, res.data
        assert "Please sign in to access this page." in res.data, res.data

        res = self.signin(next='%2Faccount%2Fprofile')
        assert self.html_title("Profile") in res.data, res
        assert "Welcome back %s" % "John Doe" in res.data, res

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_profile_applications(self, mock):
        """Test WEB user profile project page works."""
        self.create()
        self.signin(email=Fixtures.email_addr, password=Fixtures.password)
        self.new_application()
        url = '/account/%s/applications' % Fixtures.name
        res = self.app.get(url)
        assert "Projects" in res.data, res.data
        assert "Published" in res.data, res.data
        assert "Draft" in res.data, res.data
        assert Fixtures.app_name in res.data, res.data

        url = '/account/fakename/applications'
        res = self.app.get(url)
        assert res.status_code == 404, res.status_code

        url = '/account/%s/applications' % Fixtures.name2
        res = self.app.get(url)
        assert res.status_code == 403, res.status_code


    @with_context
    def test_05_update_user_profile(self):
        """Test WEB update user profile"""

        # Create an account and log in
        self.register()
        url = "/account/fake/update"
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 404, res.status_code

        # Update profile with new data
        res = self.update_profile(method="GET")
        msg = "Update your profile: %s" % "John Doe"
        assert self.html_title(msg) in res.data, res.data
        msg = 'input id="id" name="id" type="hidden" value="1"'
        assert msg in res.data, res
        assert "John Doe" in res.data, res
        assert "Save the changes" in res.data, res
        msg = '<a href="/account/johndoe/update" class="btn">Cancel</a>'
        assert  msg in res.data, res.data

        res = self.update_profile(fullname="John Doe 2",
                                  email_addr="johndoe2@example",
                                  locale="en")
        assert "Please correct the errors" in res.data, res.data


        res = self.update_profile(fullname="John Doe 2",
                                  email_addr="johndoe2@example.com",
                                  locale="en")
        title = "Update your profile: John Doe 2"
        assert self.html_title(title) in res.data, res.data
        assert "Your profile has been updated!" in res.data, res.data
        assert "John Doe 2" in res.data, res
        assert "johndoe" in res.data, res
        assert "johndoe2@example.com" in res.data, res

        # Updating the username field forces the user to re-log in
        res = self.update_profile(fullname="John Doe 2",
                                  email_addr="johndoe2@example.com",
                                  locale="en",
                                  new_name="johndoe2")
        assert "Your profile has been updated!" in res.data, res
        assert "Please sign in" in res.data, res.data

        res = self.signin(method="POST", email="johndoe2@example.com",
                          password="p4ssw0rd",
                          next="%2Faccount%2Fprofile")
        assert "Welcome back John Doe 2" in res.data, res.data
        assert "John Doe 2" in res.data, res
        assert "johndoe2" in res.data, res
        assert "johndoe2@example.com" in res.data, res

        res = self.signout()
        assert self.html_title() in res.data, res
        assert "You are now signed out" in res.data, res

        # A user must be signed in to access the update page, the page
        # the title will be the redirection to log in
        res = self.update_profile(method="GET")
        assert self.html_title("Sign in") in res.data, res
        assert "Please sign in to access this page." in res.data, res

        # A user must be signed in to access the update page, the page
        # the title will be the redirection to log in
        res = self.update_profile()
        assert self.html_title("Sign in") in res.data, res
        assert "Please sign in to access this page." in res.data, res

        self.register(fullname="new", name="new")
        url = "/account/johndoe2/update"
        res = self.app.get(url)
        assert res.status_code == 403

    @with_context
    def test_05a_get_nonexistant_app(self):
        """Test WEB get not existant project should return 404"""
        res = self.app.get('/app/nonapp', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

    @with_context
    def test_05b_get_nonexistant_app_newtask(self):
        """Test WEB get non existant project newtask should return 404"""
        res = self.app.get('/app/noapp/presenter', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        res = self.app.get('/app/noapp/newtask', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

    @with_context
    def test_05c_get_nonexistant_app_tutorial(self):
        """Test WEB get non existant project tutorial should return 404"""
        res = self.app.get('/app/noapp/tutorial', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

    @with_context
    def test_05d_get_nonexistant_app_delete(self):
        """Test WEB get non existant project delete should return 404"""
        self.register()
        # GET
        res = self.app.get('/app/noapp/delete', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.data
        # POST
        res = self.delete_application(short_name="noapp")
        assert res.status == '404 NOT FOUND', res.status

    @with_context
    def test_05d_get_nonexistant_app_update(self):
        """Test WEB get non existant project update should return 404"""
        self.register()
        # GET
        res = self.app.get('/app/noapp/update', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # POST
        res = self.update_application(short_name="noapp")
        assert res.status == '404 NOT FOUND', res.status

    @with_context
    def test_05d_get_nonexistant_app_import(self):
        """Test WEB get non existant project import should return 404"""
        self.register()
        # GET
        res = self.app.get('/app/noapp/import', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # POST
        res = self.app.post('/app/noapp/import', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

    @with_context
    def test_05d_get_nonexistant_app_task(self):
        """Test WEB get non existant project task should return 404"""
        res = self.app.get('/app/noapp/task', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Pagination
        res = self.app.get('/app/noapp/task/25', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

    @with_context
    def test_05d_get_nonexistant_app_results_json(self):
        """Test WEB get non existant project results json should return 404"""
        res = self.app.get('/app/noapp/24/results.json', follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

    @with_context
    def test_06_applications_without_apps(self):
        """Test WEB projects index without projects works"""
        # Check first without apps
        self.create_categories()
        res = self.app.get('/app', follow_redirects=True)
        assert "Projects" in res.data, res.data
        assert Fixtures.cat_1 in res.data, res.data

    @with_context
    def test_06_applications_2(self):
        """Test WEB projects index with projects"""
        self.create()

        res = self.app.get('/app', follow_redirects=True)
        assert self.html_title("Projects") in res.data, res.data
        assert "Projects" in res.data, res.data
        assert Fixtures.app_short_name in res.data, res.data


    @with_context
    def test_06_featured_apps(self):
        """Test WEB projects index shows featured projects in all the pages works"""
        self.create()

        app = db.session.query(App).get(1)
        app.featured = True
        db.session.add(app)
        db.session.commit()

        res = self.app.get('/app', follow_redirects=True)
        assert self.html_title("Projects") in res.data, res.data
        assert "Projects" in res.data, res.data
        assert '/app/test-app' in res.data, res.data
        assert '<h2><a href="/app/test-app/">My New Project</a></h2>' in res.data, res.data

        # Update one task to have more answers than expected
        task = db.session.query(Task).get(1)
        task.n_answers=1
        db.session.add(task)
        db.session.commit()
        task = db.session.query(Task).get(1)
        cat = db.session.query(Category).get(1)
        url = '/app/category/featured/'
        res = self.app.get(url, follow_redirects=True)
        assert '1 Featured Projects' in res.data, res.data

    @with_context
    @patch('pybossa.ckan.requests.get')
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_10_get_application(self, Mock, mock2):
        """Test WEB project URL/<short_name> works"""
        # Sign in and create a project
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 200,
                                   {'content-type': 'application/json'})
        Mock.return_value = html_request
        self.register()
        res = self.new_application()

        res = self.app.get('/app/sampleapp', follow_redirects=True)
        msg = "Project: Sample Project"
        assert self.html_title(msg) in res.data, res
        err_msg = "There should be a contribute button"
        assert "Start Contributing Now" in res.data, err_msg

        res = self.app.get('/app/sampleapp/settings', follow_redirects=True)
        assert res.status == '200 OK', res.status
        self.signout()

        # Now as an anonymous user
        res = self.app.get('/app/sampleapp', follow_redirects=True)
        assert self.html_title("Project: Sample Project") in res.data, res
        assert "Start Contributing Now" in res.data, err_msg
        res = self.app.get('/app/sampleapp/settings', follow_redirects=True)
        assert res.status == '200 OK', res.status
        err_msg = "Anonymous user should be redirected to sign in page"
        assert "Please sign in to access this page" in res.data, err_msg

        # Now with a different user
        self.register(fullname="Perico Palotes", name="perico")
        res = self.app.get('/app/sampleapp', follow_redirects=True)
        assert self.html_title("Project: Sample Project") in res.data, res
        assert "Start Contributing Now" in res.data, err_msg
        res = self.app.get('/app/sampleapp/settings')
        assert res.status == '403 FORBIDDEN', res.status

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_10b_application_long_description_allows_markdown(self, mock):
        """Test WEB long description markdown is supported"""
        markdown_description = u'Markdown\n======='
        self.register()
        self.new_application(long_description=markdown_description)

        res = self.app.get('/app/sampleapp', follow_redirects=True)
        data = res.data
        assert '<h1>Markdown</h1>' in data, 'Markdown text not being rendered!'

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_11_create_application(self, mock):
        """Test WEB create a project works"""
        # Create a project as an anonymous user
        res = self.new_application(method="GET")
        assert self.html_title("Sign in") in res.data, res
        assert "Please sign in to access this page" in res.data, res

        res = self.new_application()
        assert self.html_title("Sign in") in res.data, res.data
        assert "Please sign in to access this page." in res.data, res.data

        # Sign in and create a project
        res = self.register()

        res = self.new_application(method="GET")
        assert self.html_title("Create a Project") in res.data, res
        assert "Create the project" in res.data, res

        res = self.new_application(long_description='My Description')
        assert "<strong>Sample Project</strong>: Update the project" in res.data
        assert "Project created!" in res.data, res

        app = db.session.query(App).first()
        assert app.name == 'Sample Project', 'Different names %s' % app.name
        assert app.short_name == 'sampleapp', \
            'Different names %s' % app.short_name

        assert app.long_description == 'My Description', \
            "Long desc should be the same: %s" % app.long_description

        assert app.category is not None, \
            "A project should have a category after being created"

    @with_context
    def test_description_is_generated_from_long_desc(self):
        """Test WEB when creating a project, the description field is
        automatically filled in by truncating the long_description"""
        self.register()
        res = self.new_application(long_description="Hello")

        app = db.session.query(App).first()
        assert app.description == "Hello", app.description

    @with_context
    def test_description_is_generated_from_long_desc_formats(self):
        """Test WEB when when creating a project, the description generated
        from the long_description is only text (no html, no markdown)"""
        self.register()
        res = self.new_application(long_description="## Hello")

        app = db.session.query(App).first()
        assert '##' not in app.description, app.description
        assert '<h2>' not in app.description, app.description

    @with_context
    def test_description_is_generated_from_long_desc_truncates(self):
        """Test WEB when when creating a project, the description generated
        from the long_description is only text (no html, no markdown)"""
        self.register()
        res = self.new_application(long_description="a"*300)

        app = db.session.query(App).first()
        assert len(app.description) == 255, len(app.description)
        assert app.description[-3:] == '...'

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_11_a_create_application_errors(self, mock):
        """Test WEB create a project issues the errors"""
        self.register()
        # Required fields checks
        # Issue the error for the app.name
        res = self.new_application(name="")
        err_msg = "A project must have a name"
        assert "This field is required" in res.data, err_msg

        # Issue the error for the app.short_name
        res = self.new_application(short_name="")
        err_msg = "A project must have a short_name"
        assert "This field is required" in res.data, err_msg

        # Issue the error for the app.description
        res = self.new_application(long_description="")
        err_msg = "A project must have a description"
        assert "This field is required" in res.data, err_msg

        # Issue the error for the app.short_name
        res = self.new_application(short_name='$#/|')
        err_msg = "A project must have a short_name without |/$# chars"
        assert '$#&amp;\/| and space symbols are forbidden' in res.data, err_msg

        # Now Unique checks
        self.new_application()
        res = self.new_application()
        err_msg = "There should be a Unique field"
        assert "Name is already taken" in res.data, err_msg
        assert "Short Name is already taken" in res.data, err_msg

    @patch('pybossa.ckan.requests.get')
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.forms.validator.requests.get')
    def test_12_update_application(self, Mock, mock, mock_webhook):
        """Test WEB update project works"""
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 200,
                                   {'content-type': 'application/json'})
        Mock.return_value = html_request
        mock_webhook.return_value = html_request

        self.register()
        self.new_application()

        # Get the Update Project web page
        res = self.update_application(method="GET")
        msg = "Project: Sample Project &middot; Update"
        assert self.html_title(msg) in res.data, res
        msg = 'input id="id" name="id" type="hidden" value="1"'
        assert msg in res.data, res
        assert "Save the changes" in res.data, res

        # Check form validation
        res = self.update_application(new_name="",
                                      new_short_name="",
                                      new_description="New description",
                                      new_long_description='New long desc',
                                      new_hidden=True)
        assert "Please correct the errors" in res.data, res.data

        # Update the project
        res = self.update_application(new_name="New Sample Project",
                                      new_short_name="newshortname",
                                      new_description="New description",
                                      new_long_description='New long desc',
                                      new_hidden=True)
        app = db.session.query(App).first()
        assert "Project updated!" in res.data, res.data
        err_msg = "Project name not updated %s" % app.name
        assert app.name == "New Sample Project", err_msg
        err_msg = "Project short name not updated %s" % app.short_name
        assert app.short_name == "newshortname", err_msg
        err_msg = "Project description not updated %s" % app.description
        assert app.description == "New description", err_msg
        err_msg = "Project long description not updated %s" % app.long_description
        assert app.long_description == "New long desc", err_msg
        err_msg = "Project hidden not updated %s" % app.hidden
        assert app.hidden == 1, err_msg


        # Check that the owner can access it even though is hidden

        user = db.session.query(User).filter_by(name='johndoe').first()
        user.admin = False
        db.session.add(user)
        db.session.commit()
        res = self.app.get('/app/newshortname/')
        err_msg = "Owner should be able to see his hidden app"
        assert app.name in res.data, err_msg
        self.signout()

        res = self.register(fullname='Paco', name='paco')
        url = '/app/newshortname/'
        res = self.app.get(url, follow_redirects=True)
        assert "Forbidden" in res.data, res.data
        assert res.status_code == 403

        tmp = db.session.query(App).first()
        tmp.hidden = 0
        db.session.add(tmp)
        db.session.commit()

        url = '/app/newshortname/update'
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 403, res.status_code

        tmp.hidden = 1
        db.session.add(tmp)
        db.session.commit()


        user = db.session.query(User).filter_by(name='paco').first()
        user.admin = True
        db.session.add(user)
        db.session.commit()
        res = self.app.get('/app/newshortname/')
        err_msg = "Root user should be able to see his hidden app"
        assert app.name in res.data, err_msg


    @with_context
    @patch('pybossa.forms.validator.requests.get')
    def test_webhook_to_project(self, mock):
        """Test WEB update sets a webhook for the project"""
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 200,
                                   {'content-type': 'application/json'})
        mock.return_value = html_request

        self.register()
        owner = db.session.query(User).first()
        app = AppFactory.create(owner=owner)

        new_webhook = 'http://mynewserver.com/'

        self.update_application(id=app.id, short_name=app.short_name,
                                new_webhook=new_webhook)

        err_msg = "There should be an updated webhook url."
        assert app.webhook == new_webhook, err_msg


    @with_context
    @patch('pybossa.forms.validator.requests.get')
    def test_webhook_to_project_fails(self, mock):
        """Test WEB update does not set a webhook for the project"""
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 404,
                                   {'content-type': 'application/json'})
        mock.return_value = html_request

        self.register()
        owner = db.session.query(User).first()
        app = AppFactory.create(owner=owner)

        new_webhook = 'http://mynewserver.com/'

        self.update_application(id=app.id, short_name=app.short_name,
                                new_webhook=new_webhook)

        err_msg = "There should not be an updated webhook url."
        assert app.webhook != new_webhook, err_msg

    @with_context
    @patch('pybossa.forms.validator.requests.get')
    def test_webhook_to_project_conn_err(self, mock):
        """Test WEB update does not set a webhook for the project"""
        from requests.exceptions import ConnectionError
        mock.side_effect = ConnectionError

        self.register()
        owner = db.session.query(User).first()
        app = AppFactory.create(owner=owner)

        new_webhook = 'http://mynewserver.com/'

        res = self.update_application(id=app.id, short_name=app.short_name,
                                      new_webhook=new_webhook)

        err_msg = "There should not be an updated webhook url."
        assert app.webhook != new_webhook, err_msg


    @with_context
    @patch('pybossa.forms.validator.requests.get')
    def test_add_password_to_project(self, mock_webhook):
        """Test WEB update sets a password for the project"""
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 200,
                                   {'content-type': 'application/json'})
        mock_webhook.return_value = html_request
        self.register()
        owner = db.session.query(User).first()
        app = AppFactory.create(owner=owner)

        self.update_application(id=app.id, short_name=app.short_name,
                                new_password='mysecret')

        assert app.needs_password(), 'Password not set"'


    @with_context
    @patch('pybossa.forms.validator.requests.get')
    def test_remove_password_from_project(self, mock_webhook):
        """Test WEB update removes the password of the project"""
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 200,
                                   {'content-type': 'application/json'})
        mock_webhook.return_value = html_request
        self.register()
        owner = db.session.query(User).first()
        app = AppFactory.create(info={'passwd_hash': 'mysecret'}, owner=owner)

        self.update_application(id=app.id, short_name=app.short_name,
                                new_password='')

        assert not app.needs_password(), 'Password not deleted'


    @with_context
    def test_update_application_errors(self):
        """Test WEB update form validation issues the errors"""
        self.register()
        self.new_application()

        res = self.update_application(new_name="")
        assert "This field is required" in res.data

        res = self.update_application(new_short_name="")
        assert "This field is required" in res.data

        res = self.update_application(new_description="")
        assert "You must provide a description." in res.data

        res = self.update_application(new_description="a"*256)
        assert "Field cannot be longer than 255 characters." in res.data

        res = self.update_application(new_long_description="")
        assert "This field is required" not in res.data


    @with_context
    @patch('pybossa.ckan.requests.get')
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_13_hidden_applications(self, Mock, mock):
        """Test WEB hidden project works"""
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 200,
                                   {'content-type': 'application/json'})
        Mock.return_value = html_request
        self.register()
        self.new_application()
        self.update_application(new_hidden=True)
        self.signout()

        res = self.app.get('/app/', follow_redirects=True)
        assert "Sample Project" not in res.data, res

        res = self.app.get('/app/sampleapp', follow_redirects=True)
        err_msg = "Hidden apps should return a 403"
        res.status_code == 403, err_msg

    @with_context
    @patch('pybossa.ckan.requests.get')
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_13a_hidden_applications_owner(self, Mock, mock):
        """Test WEB hidden projects are shown to their owners"""
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 200,
                                   {'content-type': 'application/json'})
        Mock.return_value = html_request

        self.register()
        self.new_application()
        self.update_application(new_hidden=True)

        res = self.app.get('/app/', follow_redirects=True)
        assert "Sample Project" not in res.data, ("Projects should be hidden"
                                              "in the index")

        res = self.app.get('/app/sampleapp', follow_redirects=True)
        assert "Sample Project" in res.data, ("Project should be shown to"
                                          "the owner")

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_14_delete_application(self, mock):
        """Test WEB delete project works"""
        self.create()
        self.register()
        self.new_application()
        res = self.delete_application(method="GET")
        msg = "Project: Sample Project &middot; Delete"
        assert self.html_title(msg) in res.data, res
        assert "No, do not delete it" in res.data, res

        app = db.session.query(App).filter_by(short_name='sampleapp').first()
        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        res = self.delete_application(method="GET")
        msg = "Project: Sample Project &middot; Delete"
        assert self.html_title(msg) in res.data, res
        assert "No, do not delete it" in res.data, res

        res = self.delete_application()
        assert "Project deleted!" in res.data, res

        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        res = self.delete_application(short_name=Fixtures.app_short_name)
        assert res.status_code == 403, res.status_code


    @with_context
    def test_15_twitter_email_warning(self):
        """Test WEB Twitter email warning works"""
        # This test assumes that the user allows Twitter to authenticate,
        #  returning a valid resp. The only difference is a user object
        #  without a password
        #  Register a user and sign out
        user = User(name="tester", passwd_hash="tester",
                          fullname="tester",
                          email_addr="tester")
        user.set_password('tester')
        db.session.add(user)
        db.session.commit()
        db.session.query(User).all()

        # Sign in again and check the warning message
        self.signin(email="tester", password="tester")
        res = self.app.get('/', follow_redirects=True)
        msg = "Please update your e-mail address in your profile page, " \
              "right now it is empty!"
        user = db.session.query(User).get(1)
        assert msg in res.data, res.data

    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_16_task_status_completed(self, mock):
        """Test WEB Task Status Completed works"""
        self.register()
        self.new_application()

        app = db.session.query(App).first()
        # We use a string here to check that it works too
        task = Task(app_id=app.id, n_answers = 10)
        db.session.add(task)
        db.session.commit()

        res = self.app.get('app/%s/tasks/browse' % (app.short_name),
                           follow_redirects=True)
        dom = BeautifulSoup(res.data)
        assert "Sample Project" in res.data, res.data
        assert '0 of 10' in res.data, res.data
        err_msg = "Download button should be disabled"
        assert dom.find(id='nothingtodownload') is not None, err_msg

        for i in range(5):
            task_run = TaskRun(app_id=app.id, task_id=1,
                                     info={'answer': 1})
            db.session.add(task_run)
            db.session.commit()
            self.app.get('api/app/%s/newtask' % app.id)

        res = self.app.get('app/%s/tasks/browse' % (app.short_name),
                           follow_redirects=True)
        dom = BeautifulSoup(res.data)
        assert "Sample Project" in res.data, res.data
        assert '5 of 10' in res.data, res.data
        err_msg = "Download Partial results button should be shown"
        assert dom.find(id='partialdownload') is not None, err_msg

        for i in range(5):
            task_run = TaskRun(app_id=app.id, task_id=1,
                                     info={'answer': 1})
            db.session.add(task_run)
            db.session.commit()
            self.app.get('api/app/%s/newtask' % app.id)

        self.signout()

        app = db.session.query(App).first()

        res = self.app.get('app/%s/tasks/browse' % (app.short_name),
                           follow_redirects=True)
        assert "Sample Project" in res.data, res.data
        msg = 'Task <span class="label label-success">#1</span>'
        assert msg in res.data, res.data
        assert '10 of 10' in res.data, res.data
        dom = BeautifulSoup(res.data)
        err_msg = "Download Full results button should be shown"
        assert dom.find(id='fulldownload') is not None, err_msg

        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        res = self.app.get('app/%s/tasks/browse' % (app.short_name),
                           follow_redirects=True)
        assert res.status_code == 401, res.status_code

        self.create()
        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        res = self.app.get('app/%s/tasks/browse' % (app.short_name),
                           follow_redirects=True)
        assert res.status_code == 403, res.status_code


    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_17_export_task_runs(self, mock):
        """Test WEB TaskRun export works"""
        self.register()
        self.new_application()

        app = db.session.query(App).first()
        task = Task(app_id=app.id, n_answers = 10)
        db.session.add(task)
        db.session.commit()

        for i in range(10):
            task_run = TaskRun(app_id=app.id, task_id=1, info={'answer': 1})
            db.session.add(task_run)
            db.session.commit()

        app = db.session.query(App).first()
        res = self.app.get('app/%s/%s/results.json' % (app.short_name, 1),
                           follow_redirects=True)
        data = json.loads(res.data)
        assert len(data) == 10, data
        for tr in data:
            assert tr['info']['answer'] == 1, tr

        # Check with correct app but wrong task id
        res = self.app.get('app/%s/%s/results.json' % (app.short_name, 5000),
                           follow_redirects=True)
        assert res.status_code == 404, res.status_code

        # Check with hidden app: owner should have access to it
        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        res = self.app.get('app/%s/%s/results.json' % (app.short_name, 1),
                           follow_redirects=True)
        data = json.loads(res.data)
        assert len(data) == 10, data
        for tr in data:
            assert tr['info']['answer'] == 1, tr
        self.signout()

        # Check with hidden app: non-owner should not have access to it
        self.register(fullname="Non Owner", name="nonowner")
        res = self.app.get('app/%s/%s/results.json' % (app.short_name, 1),
                           follow_redirects=True)
        assert res.status_code == 403, res.data
        assert "Forbidden" in res.data, res.data

        # Check with hidden app: anonymous should not have access to it
        self.signout()
        res = self.app.get('app/%s/%s/results.json' % (app.short_name, 1),
                           follow_redirects=True)
        assert res.status_code == 401, res.data
        assert "Unauthorized" in res.data, res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_18_task_status_wip(self, mock):
        """Test WEB Task Status on going works"""
        self.register()
        self.new_application()

        app = db.session.query(App).first()
        task = Task(app_id=app.id, n_answers = 10)
        db.session.add(task)
        db.session.commit()
        self.signout()

        app = db.session.query(App).first()

        res = self.app.get('app/%s/tasks/browse' % (app.short_name),
                           follow_redirects=True)
        assert "Sample Project" in res.data, res.data
        msg = 'Task <span class="label label-info">#1</span>'
        assert msg in res.data, res.data
        assert '0 of 10' in res.data, res.data

        # For a non existing page
        res = self.app.get('app/%s/tasks/browse/5000' % (app.short_name),
                           follow_redirects=True)
        assert res.status_code == 404, res.status_code


    @with_context
    def test_19_app_index_categories(self):
        """Test WEB Project Index categories works"""
        self.register()
        self.create()
        self.signout()

        res = self.app.get('app', follow_redirects=True)
        assert "Projects" in res.data, res.data
        assert Fixtures.cat_1 in res.data, res.data

        task = db.session.query(Task).get(1)
        # Update one task to have more answers than expected
        task.n_answers=1
        db.session.add(task)
        db.session.commit()
        task = db.session.query(Task).get(1)
        cat = db.session.query(Category).get(1)
        url = '/app/category/%s/' % Fixtures.cat_1
        res = self.app.get(url, follow_redirects=True)
        tmp = '1 %s Projects' % Fixtures.cat_1
        assert tmp in res.data, res

    @with_context
    def test_app_index_categories_pagination(self):
        """Test WEB Project Index categories pagination works"""
        from flask import current_app
        n_apps = current_app.config.get('APPS_PER_PAGE')
        current_app.config['APPS_PER_PAGE'] = 1
        category = CategoryFactory.create(name='category', short_name='cat')
        for project in AppFactory.create_batch(2, category=category):
            TaskFactory.create(app=project)
        page1 = self.app.get('/app/category/%s/' % category.short_name)
        page2 = self.app.get('/app/category/%s/page/2/' % category.short_name)
        current_app.config['APPS_PER_PAGE'] = n_apps

        assert '<a href="/app/category/cat/page/2/">Next &raquo;</a>' in page1.data
        assert page2.status_code == 200, page2.status_code
        assert '<a href="/app/category/cat/">&laquo; Prev </a>' in page2.data


    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_20_app_index_published(self, mock):
        """Test WEB Project Index published works"""
        self.register()
        self.new_application()
        self.update_application(new_category_id="1")
        app = db.session.query(App).first()
        info = dict(task_presenter="some html")
        app.info = info
        db.session.commit()
        task = Task(app_id=app.id, n_answers = 10)
        db.session.add(task)
        db.session.commit()
        self.signout()

        res = self.app.get('app', follow_redirects=True)
        assert "%s Projects" % Fixtures.cat_1 in res.data, res.data
        assert "draft" not in res.data, res.data
        assert "Sample Project" in res.data, res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_20_app_index_draft(self, mock):
        """Test WEB Project Index draft works"""
        # Create root
        self.register()
        self.new_application()
        self.signout()
        # Create a user
        self.register(fullname="jane", name="jane", email="jane@jane.com")
        self.signout()

        # As Anonymous
        res = self.app.get('/app/category/draft', follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "Anonymous should not see draft apps"
        assert dom.find(id='signin') is not None, err_msg

        # As authenticated but not admin
        self.signin(email="jane@jane.com", password="p4ssw0rd")
        res = self.app.get('/app/category/draft', follow_redirects=True)
        assert res.status_code == 403, "Non-admin should not see draft apps"
        self.signout()

        # As Admin
        self.signin()
        res = self.app.get('/app/category/draft', follow_redirects=True)
        assert "project-published" not in res.data, res.data
        assert "draft" in res.data, res.data
        assert "Sample Project" in res.data, res.data
        assert '1 Draft Projects' in res.data, res.data

    @with_context
    def test_21_get_specific_ongoing_task_anonymous(self):
        """Test WEB get specific ongoing task_id for
        a project works as anonymous"""
        self.create()
        self.delete_task_runs()
        app = db.session.query(App).first()
        task = db.session.query(Task)\
                 .filter(App.id == app.id)\
                 .first()
        res = self.app.get('app/%s/task/%s' % (app.short_name, task.id),
                           follow_redirects=True)
        assert 'TaskPresenter' in res.data, res.data
        msg = "?next=%2Fapp%2F" + app.short_name + "%2Ftask%2F" + str(task.id)
        assert msg in res.data, res.data

        # Try with a hidden app
        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        res = self.app.get('app/%s/task/%s' % (app.short_name, task.id),
                           follow_redirects=True)
        assert 'Unauthorized' in res.data, res.data
        assert res.status_code == 401, res.status_code
        # Try with only registered users
        app.allow_anonymous_contributors = False
        app.hidden = 0
        db.session.add(app)
        db.session.commit()
        res = self.app.get('app/%s/task/%s' % (app.short_name, task.id),
                           follow_redirects=True)
        assert "sign in to participate" in res.data

    @with_context
    def test_23_get_specific_ongoing_task_user(self):
        """Test WEB get specific ongoing task_id for a project works as an user"""
        self.create()
        self.delete_task_runs()
        self.register()
        self.signin()
        app = db.session.query(App).first()
        task = db.session.query(Task)\
                 .filter(App.id == app.id)\
                 .first()
        res = self.app.get('app/%s/task/%s' % (app.short_name, task.id),
                           follow_redirects=True)
        assert 'TaskPresenter' in res.data, res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_25_get_wrong_task_app(self, mock):
        """Test WEB get wrong task.id for a project works"""
        self.create()
        app1 = db.session.query(App).get(1)
        app1_short_name = app1.short_name

        db.session.query(Task)\
                  .filter(Task.app_id == 1)\
                  .first()

        self.register()
        self.new_application()
        app2 = db.session.query(App).get(2)
        self.new_task(app2.id)
        task2 = db.session.query(Task)\
                  .filter(Task.app_id == 2)\
                  .first()
        task2_id = task2.id
        self.signout()

        res = self.app.get('/app/%s/task/%s' % (app1_short_name, task2_id))
        assert "Error" in res.data, res.data
        msg = "This task does not belong to %s" % app1_short_name
        assert msg in res.data, res.data

    @with_context
    def test_26_tutorial_signed_user(self):
        """Test WEB tutorials work as signed in user"""
        self.create()
        app1 = db.session.query(App).get(1)
        app1.info = dict(tutorial="some help")
        db.session.commit()
        self.register()
        # First time accessing the app should redirect me to the tutorial
        res = self.app.get('/app/test-app/newtask', follow_redirects=True)
        err_msg = "There should be some tutorial for the project"
        assert "some help" in res.data, err_msg
        # Second time should give me a task, and not the tutorial
        res = self.app.get('/app/test-app/newtask', follow_redirects=True)
        assert "some help" not in res.data

        # Check if the tutorial can be accessed directly
        res = self.app.get('/app/test-app/tutorial', follow_redirects=True)
        err_msg = "There should be some tutorial for the project"
        assert "some help" in res.data, err_msg

        # Hidden app
        app1.hidden = 1
        db.session.add(app1)
        db.session.commit()
        url = '/app/%s/tutorial' % app1.short_name
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 403, res.status_code


    @with_context
    def test_27_tutorial_anonymous_user(self):
        """Test WEB tutorials work as an anonymous user"""
        self.create()
        app1 = db.session.query(App).get(1)
        app1.info = dict(tutorial="some help")
        db.session.commit()
        # First time accessing the app should redirect me to the tutorial
        res = self.app.get('/app/test-app/newtask', follow_redirects=True)
        err_msg = "There should be some tutorial for the project"
        assert "some help" in res.data, err_msg
        # Second time should give me a task, and not the tutorial
        res = self.app.get('/app/test-app/newtask', follow_redirects=True)
        assert "some help" not in res.data

        # Check if the tutorial can be accessed directly
        res = self.app.get('/app/test-app/tutorial', follow_redirects=True)
        err_msg = "There should be some tutorial for the project"
        assert "some help" in res.data, err_msg

        # Hidden app
        app1.hidden = 1
        db.session.add(app1)
        db.session.commit()
        res = self.app.get('/app/test-app/tutorial', follow_redirects=True)
        assert res.status_code == 401, res.status_code

    @with_context
    def test_28_non_tutorial_signed_user(self):
        """Test WEB project without tutorial work as signed in user"""
        self.create()
        db.session.commit()
        self.register()
        # First time accessing the app should redirect me to the tutorial
        res = self.app.get('/app/test-app/newtask', follow_redirects=True)
        err_msg = "There should not be a tutorial for the project"
        assert "some help" not in res.data, err_msg
        # Second time should give me a task, and not the tutorial
        res = self.app.get('/app/test-app/newtask', follow_redirects=True)
        assert "some help" not in res.data

    @with_context
    def test_29_tutorial_anonymous_user(self):
        """Test WEB project without tutorials work as an anonymous user"""
        self.create()
        db.session.commit()
        self.register()
        # First time accessing the app should redirect me to the tutorial
        res = self.app.get('/app/test-app/newtask', follow_redirects=True)
        err_msg = "There should not be a tutorial for the project"
        assert "some help" not in res.data, err_msg
        # Second time should give me a task, and not the tutorial
        res = self.app.get('/app/test-app/newtask', follow_redirects=True)
        assert "some help" not in res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_30_app_id_owner(self, mock):
        """Test WEB project settings page shows the ID to the owner"""
        self.register()
        self.new_application()

        res = self.app.get('/app/sampleapp/settings', follow_redirects=True)
        assert "Sample Project" in res.data, ("Project should be shown to "
                                          "the owner")
        msg = '<strong><i class="icon-cog"></i> ID</strong>: 1'
        err_msg = "Project ID should be shown to the owner"
        assert msg in res.data, err_msg

        self.signout()
        self.create()
        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        res = self.app.get('/app/sampleapp/settings', follow_redirects=True)
        assert res.status_code == 403, res.status_code

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.ckan.requests.get')
    def test_30_app_id_anonymous_user(self, Mock, mock):
        """Test WEB project page does not show the ID to anonymous users"""
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 200,
                                   {'content-type': 'application/json'})
        Mock.return_value = html_request

        self.register()
        self.new_application()
        self.signout()

        res = self.app.get('/app/sampleapp', follow_redirects=True)
        assert "Sample Project" in res.data, ("Project name should be shown"
                                          " to users")
        assert '<strong><i class="icon-cog"></i> ID</strong>: 1' not in \
            res.data, "Project ID should be shown to the owner"

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_31_user_profile_progress(self, mock):
        """Test WEB user progress profile page works"""
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        task = Task(app_id=app.id, n_answers = 10)
        db.session.add(task)
        db.session.commit()
        for i in range(10):
            task_run = TaskRun(app_id=app.id, task_id=1, user_id=1,
                                     info={'answer': 1})
            db.session.add(task_run)
            db.session.commit()
            self.app.get('api/app/%s/newtask' % app.id)

        res = self.app.get('account/johndoe', follow_redirects=True)
        assert "Sample Project" in res.data, res.data
        assert "Contribute!" in res.data, "There should be a Contribute button"

    @with_context
    def test_32_oauth_password(self):
        """Test WEB user sign in without password works"""
        user = User(email_addr="johndoe@johndoe.com",
                          name="John Doe",
                          passwd_hash=None,
                          fullname="johndoe",
                          api_key="api-key")
        db.session.add(user)
        db.session.commit()
        res = self.signin()
        assert "Ooops, we didn't find you in the system" in res.data, res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.importers.requests.get')
    def test_33_bulk_csv_import_forbidden(self, Mock, mock):
        """Test WEB bulk import Forbidden works"""
        forbidden_request = FakeRequest('Forbidden', 403,
                                           {'content-type': 'text/csv'})
        Mock.return_value = forbidden_request
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'formtype': 'csv', 'form_name': 'csv'},
                            follow_redirects=True)
        msg = "Oops! It looks like you don't have permission to access that file"
        assert msg in res.data, res.data

    @with_context
    @patch('pybossa.importers.requests.get')
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_34_bulk_csv_import_non_html(self, Mock, mock):
        """Test WEB bulk import non html works"""
        html_request = FakeRequest('Not a CSV', 200,
                                   {'content-type': 'text/html'})
        Mock.return_value = html_request
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'form_name': 'csv'},
                            follow_redirects=True)
        assert "Oops! That file doesn't look like the right file." in res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.importers.requests.get')
    def test_35_bulk_csv_import_non_html(self, Mock, mock):
        """Test WEB bulk import non html works"""
        empty_file = FakeRequest('CSV,with,no,content\n', 200,
                                 {'content-type': 'text/plain'})
        Mock.return_value = empty_file
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'formtype': 'csv', 'form_name': 'csv'},
                            follow_redirects=True)
        assert "Oops! It looks like the file is empty." in res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.importers.requests.get')
    def test_36_bulk_csv_import_dup_header(self, Mock, mock):
        """Test WEB bulk import duplicate header works"""
        empty_file = FakeRequest('Foo,Bar,Foo\n1,2,3', 200,
                                 {'content-type': 'text/plain'})
        Mock.return_value = empty_file
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'formtype': 'csv', 'form_name': 'csv'},
                            follow_redirects=True)
        msg = "The file you uploaded has two headers with the same name"
        assert msg in res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.importers.requests.get')
    def test_37_bulk_csv_import_no_column_names(self, Mock, mock):
        """Test WEB bulk import no column names works"""
        empty_file = FakeRequest('Foo,Bar,Baz\n1,2,3', 200,
                                 {'content-type': 'text/plain'})
        Mock.return_value = empty_file
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'formtype': 'csv', 'form_name': 'csv'},
                            follow_redirects=True)
        task = db.session.query(Task).first()
        assert {u'Bar': u'2', u'Foo': u'1', u'Baz': u'3'} == task.info
        assert "1 new task was imported successfully" in res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.importers.requests.get')
    def test_38_bulk_csv_import_with_column_name(self, Mock, mock):
        """Test WEB bulk import with column name works"""
        empty_file = FakeRequest('Foo,Bar,priority_0\n1,2,3', 200,
                                 {'content-type': 'text/plain'})
        Mock.return_value = empty_file
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'formtype': 'csv', 'form_name': 'csv'},
                            follow_redirects=True)
        task = db.session.query(Task).first()
        assert {u'Bar': u'2', u'Foo': u'1'} == task.info
        assert task.priority_0 == 3
        assert "1 new task was imported successfully" in res.data

        # Check that only new items are imported
        empty_file = FakeRequest('Foo,Bar,priority_0\n1,2,3\n4,5,6', 200,
                                 {'content-type': 'text/plain'})
        Mock.return_value = empty_file
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'formtype': 'csv', 'form_name': 'csv'},
                            follow_redirects=True)
        app = db.session.query(App).first()
        assert len(app.tasks) == 2, "There should be only 2 tasks"
        n = 0
        csv_tasks = [{u'Foo': u'1', u'Bar': u'2'}, {u'Foo': u'4', u'Bar': u'5'}]
        for t in app.tasks:
            assert t.info == csv_tasks[n], "The task info should be the same"
            n += 1

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.importers.requests.get')
    def test_38_bulk_gdocs_import(self, Mock, mock):
        """Test WEB bulk GDocs import works."""
        empty_file = FakeRequest('Foo,Bar,priority_0\n1,2,3', 200,
                                 {'content-type': 'text/plain'})
        Mock.return_value = empty_file
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'googledocs_url': 'http://drive.google.com',
                                       'formtype': 'gdocs', 'form_name': 'gdocs'},
                            follow_redirects=True)
        task = db.session.query(Task).first()
        assert {u'Bar': u'2', u'Foo': u'1'} == task.info
        assert task.priority_0 == 3
        assert "1 new task was imported successfully" in res.data

        # Check that only new items are imported
        empty_file = FakeRequest('Foo,Bar,priority_0\n1,2,3\n4,5,6', 200,
                                 {'content-type': 'text/plain'})
        Mock.return_value = empty_file
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'googledocs_url': 'http://drive.google.com',
                                       'formtype': 'gdocs', 'form_name': 'gdocs'},
                            follow_redirects=True)
        app = db.session.query(App).first()
        assert len(app.tasks) == 2, "There should be only 2 tasks"
        n = 0
        csv_tasks = [{u'Foo': u'1', u'Bar': u'2'}, {u'Foo': u'4', u'Bar': u'5'}]
        for t in app.tasks:
            assert t.info == csv_tasks[n], "The task info should be the same"
            n += 1

        # Check that only new items are imported
        empty_file = FakeRequest('Foo,Bar,priority_0\n1,2,3\n4,5,6', 200,
                                 {'content-type': 'text/plain'})
        Mock.return_value = empty_file
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'googledocs_url': 'http://drive.google.com',
                                       'formtype': 'gdocs', 'form_name': 'gdocs'},
                            follow_redirects=True)
        app = db.session.query(App).first()
        assert len(app.tasks) == 2, "There should be only 2 tasks"
        n = 0
        csv_tasks = [{u'Foo': u'1', u'Bar': u'2'}, {u'Foo': u'4', u'Bar': u'5'}]
        for t in app.tasks:
            assert t.info == csv_tasks[n], "The task info should be the same"
            n += 1
        assert "no new records" in res.data, res.data

    @with_context
    def test_39_google_oauth_creation(self):
        """Test WEB Google OAuth creation of user works"""
        fake_response = {
            u'access_token': u'access_token',
            u'token_type': u'Bearer',
            u'expires_in': 3600,
            u'id_token': u'token'}

        fake_user = {
            u'family_name': u'Doe', u'name': u'John Doe',
            u'picture': u'https://goo.gl/img.jpg',
            u'locale': u'en',
            u'gender': u'male',
            u'email': u'john@gmail.com',
            u'birthday': u'0000-01-15',
            u'link': u'https://plus.google.com/id',
            u'given_name': u'John',
            u'id': u'111111111111111111111',
            u'verified_email': True}

        from pybossa.view import google
        response_user = google.manage_user(fake_response['access_token'],
                                           fake_user, None)

        user = db.session.query(User).get(1)

        assert user.email_addr == response_user.email_addr, response_user

    @with_context
    def test_40_google_oauth_creation(self):
        """Test WEB Google OAuth detects same user name/email works"""
        fake_response = {
            u'access_token': u'access_token',
            u'token_type': u'Bearer',
            u'expires_in': 3600,
            u'id_token': u'token'}

        fake_user = {
            u'family_name': u'Doe', u'name': u'John Doe',
            u'picture': u'https://goo.gl/img.jpg',
            u'locale': u'en',
            u'gender': u'male',
            u'email': u'john@gmail.com',
            u'birthday': u'0000-01-15',
            u'link': u'https://plus.google.com/id',
            u'given_name': u'John',
            u'id': u'111111111111111111111',
            u'verified_email': True}

        self.register()
        self.signout()

        from pybossa.view import google
        response_user = google.manage_user(fake_response['access_token'],
                                           fake_user, None)

        assert response_user is None, response_user

    @with_context
    def test_39_facebook_oauth_creation(self):
        """Test WEB Facebook OAuth creation of user works"""
        fake_response = {
            u'access_token': u'access_token',
            u'token_type': u'Bearer',
            u'expires_in': 3600,
            u'id_token': u'token'}

        fake_user = {
            u'username': u'teleyinex',
            u'first_name': u'John',
            u'last_name': u'Doe',
            u'verified': True,
            u'name': u'John Doe',
            u'locale': u'en_US',
            u'gender': u'male',
            u'email': u'johndoe@example.com',
            u'quotes': u'"quote',
            u'link': u'http://www.facebook.com/johndoe',
            u'timezone': 1,
            u'updated_time': u'2011-11-11T12:33:52+0000',
            u'id': u'11111'}

        from pybossa.view import facebook
        response_user = facebook.manage_user(fake_response['access_token'],
                                             fake_user, None)

        user = db.session.query(User).get(1)

        assert user.email_addr == response_user.email_addr, response_user

    @with_context
    def test_40_facebook_oauth_creation(self):
        """Test WEB Facebook OAuth detects same user name/email works"""
        fake_response = {
            u'access_token': u'access_token',
            u'token_type': u'Bearer',
            u'expires_in': 3600,
            u'id_token': u'token'}

        fake_user = {
            u'username': u'teleyinex',
            u'first_name': u'John',
            u'last_name': u'Doe',
            u'verified': True,
            u'name': u'John Doe',
            u'locale': u'en_US',
            u'gender': u'male',
            u'email': u'johndoe@example.com',
            u'quotes': u'"quote',
            u'link': u'http://www.facebook.com/johndoe',
            u'timezone': 1,
            u'updated_time': u'2011-11-11T12:33:52+0000',
            u'id': u'11111'}

        self.register()
        self.signout()

        from pybossa.view import facebook
        response_user = facebook.manage_user(fake_response['access_token'],
                                             fake_user, None)

        assert response_user is None, response_user

    @with_context
    def test_39_twitter_oauth_creation(self):
        """Test WEB Twitter OAuth creation of user works"""
        fake_response = {
            u'access_token': {u'oauth_token': u'oauth_token',
                              u'oauth_token_secret': u'oauth_token_secret'},
            u'token_type': u'Bearer',
            u'expires_in': 3600,
            u'id_token': u'token'}

        fake_user = {u'screen_name': u'johndoe',
                     u'user_id': u'11111'}

        from pybossa.view import twitter
        response_user = twitter.manage_user(fake_response['access_token'],
                                            fake_user, None)

        user = db.session.query(User).get(1)

        assert user.email_addr == response_user.email_addr, response_user

        res = self.signin(email=user.email_addr, password='wrong')
        msg = "It seems like you signed up with your Twitter account"
        assert msg in res.data, msg

    @with_context
    def test_40_twitter_oauth_creation(self):
        """Test WEB Twitter OAuth detects same user name/email works"""
        fake_response = {
            u'access_token': {u'oauth_token': u'oauth_token',
                              u'oauth_token_secret': u'oauth_token_secret'},
            u'token_type': u'Bearer',
            u'expires_in': 3600,
            u'id_token': u'token'}

        fake_user = {u'screen_name': u'johndoe',
                     u'user_id': u'11111'}

        self.register()
        self.signout()

        from pybossa.view import twitter
        response_user = twitter.manage_user(fake_response['access_token'],
                                            fake_user, None)

        assert response_user is None, response_user

    @with_context
    def test_41_password_change(self):
        """Test WEB password changing"""
        password = "mehpassword"
        self.register(password=password)
        res = self.app.post('/account/johndoe/update',
                            data={'current_password': password,
                                  'new_password': "p4ssw0rd",
                                  'confirm': "p4ssw0rd",
                                  'btn': 'Password'},
                            follow_redirects=True)
        assert "Yay, you changed your password succesfully!" in res.data, res.data

        password = "p4ssw0rd"
        self.signin(password=password)
        res = self.app.post('/account/johndoe/update',
                            data={'current_password': "wrongpassword",
                                  'new_password': "p4ssw0rd",
                                  'confirm': "p4ssw0rd",
                                  'btn': 'Password'},
                            follow_redirects=True)
        msg = "Your current password doesn't match the one in our records"
        assert msg in res.data

        res = self.app.post('/account/johndoe/update',
                            data={'current_password': '',
                                  'new_password':'',
                                  'confirm': '',
                                  'btn': 'Password'},
                            follow_redirects=True)
        msg = "Please correct the errors"
        assert msg in res.data

    @with_context
    def test_42_password_link(self):
        """Test WEB visibility of password change link"""
        self.register()
        res = self.app.get('/account/johndoe/update')
        assert "Change your Password" in res.data
        user = User.query.get(1)
        user.twitter_user_id = 1234
        db.session.add(user)
        db.session.commit()
        res = self.app.get('/account/johndoe/update')
        assert "Change your Password" not in res.data, res.data

    @with_context
    def test_43_terms_of_use_and_data(self):
        """Test WEB terms of use is working"""
        res = self.app.get('account/signin', follow_redirects=True)
        assert "/help/terms-of-use" in res.data, res.data
        assert "http://opendatacommons.org/licenses/by/" in res.data, res.data

        res = self.app.get('account/register', follow_redirects=True)
        assert "http://okfn.org/terms-of-use/" in res.data, res.data
        assert "http://opendatacommons.org/licenses/by/" in res.data, res.data

    @with_context
    @patch('pybossa.view.account.signer.loads')
    def test_44_password_reset_key_errors(self, Mock):
        """Test WEB password reset key errors are caught"""
        self.register()
        user = User.query.get(1)
        userdict = {'user': user.name, 'password': user.passwd_hash}
        fakeuserdict = {'user': user.name, 'password': 'wronghash'}
        fakeuserdict_err = {'user': user.name, 'passwd': 'some'}
        fakeuserdict_form = {'user': user.name, 'passwd': 'p4ssw0rD'}
        key = signer.dumps(userdict, salt='password-reset')
        returns = [BadSignature('Fake Error'), BadSignature('Fake Error'), userdict,
                   fakeuserdict, userdict, userdict, fakeuserdict_err]

        def side_effects(*args, **kwargs):
            result = returns.pop(0)
            if isinstance(result, BadSignature):
                raise result
            return result
        Mock.side_effect = side_effects
        # Request with no key
        res = self.app.get('/account/reset-password', follow_redirects=True)
        assert 403 == res.status_code
        # Request with invalid key
        res = self.app.get('/account/reset-password?key=foo', follow_redirects=True)
        assert 403 == res.status_code
        # Request with key exception
        res = self.app.get('/account/reset-password?key=%s' % (key), follow_redirects=True)
        assert 403 == res.status_code
        res = self.app.get('/account/reset-password?key=%s' % (key), follow_redirects=True)
        assert 200 == res.status_code
        res = self.app.get('/account/reset-password?key=%s' % (key), follow_redirects=True)
        assert 403 == res.status_code

        # Check validation
        res = self.app.post('/account/reset-password?key=%s' % (key),
                            data={'new_password': '',
                                  'confirm': '#4a4'},
                            follow_redirects=True)

        assert "Please correct the errors" in res.data, res.data

        res = self.app.post('/account/reset-password?key=%s' % (key),
                            data={'new_password': 'p4ssw0rD',
                                  'confirm': 'p4ssw0rD'},
                            follow_redirects=True)

        assert "You reset your password successfully!" in res.data

        # Request without password
        res = self.app.get('/account/reset-password?key=%s' % (key), follow_redirects=True)
        assert 403 == res.status_code

    @with_context
    @patch('pybossa.view.account.mail_queue', autospec=True)
    @patch('pybossa.view.account.signer')
    def test_45_password_reset_link(self, signer, queue):
        """Test WEB password reset email form"""
        res = self.app.post('/account/forgot-password',
                            data={'email_addr': "johndoe@example.com"},
                            follow_redirects=True)
        assert ("We don't have this email in our records. You may have"
                " signed up with a different email or used Twitter, "
                "Facebook, or Google to sign-in") in res.data

        self.register()
        self.register(name='janedoe')
        self.register(name='google')
        self.register(name='facebook')
        user = User.query.get(1)
        jane = User.query.get(2)
        jane.twitter_user_id = 10
        google = User.query.get(3)
        google.google_user_id = 103
        facebook = User.query.get(4)
        facebook.facebook_user_id = 104
        db.session.add_all([jane, google, facebook])
        db.session.commit()

        data = {'password': user.passwd_hash, 'user': user.name}
        self.app.post('/account/forgot-password',
                      data={'email_addr': user.email_addr},
                      follow_redirects=True)
        signer.dumps.assert_called_with(data, salt='password-reset')
        enqueue_call = queue.enqueue.call_args_list[0]
        assert send_mail == enqueue_call[0][0], "send_mail not called"
        assert 'Click here to recover your account' in enqueue_call[0][1]['body']

        data = {'password': jane.passwd_hash, 'user': jane.name}
        self.app.post('/account/forgot-password',
                      data={'email_addr': 'janedoe@example.com'},
                      follow_redirects=True)
        enqueue_call = queue.enqueue.call_args_list[1]
        assert send_mail == enqueue_call[0][0], "send_mail not called"
        assert 'your Twitter account to ' in enqueue_call[0][1]['body']

        data = {'password': google.passwd_hash, 'user': google.name}
        self.app.post('/account/forgot-password',
                      data={'email_addr': 'google@example.com'},
                      follow_redirects=True)
        enqueue_call = queue.enqueue.call_args_list[2]
        assert send_mail == enqueue_call[0][0], "send_mail not called"
        assert 'your Google account to ' in enqueue_call[0][1]['body']

        data = {'password': facebook.passwd_hash, 'user': facebook.name}
        self.app.post('/account/forgot-password',
                      data={'email_addr': 'facebook@example.com'},
                      follow_redirects=True)
        enqueue_call = queue.enqueue.call_args_list[3]
        assert send_mail == enqueue_call[0][0], "send_mail not called"
        assert 'your Facebook account to ' in enqueue_call[0][1]['body']

        # Test with not valid form
        res = self.app.post('/account/forgot-password',
                            data={'email_addr': ''},
                            follow_redirects=True)
        msg = "Something went wrong, please correct the errors"
        assert msg in res.data, res.data


    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_46_tasks_exists(self, mock):
        """Test WEB tasks page works."""
        self.register()
        self.new_application()
        res = self.app.get('/app/sampleapp/tasks/', follow_redirects=True)
        assert "Edit the task presenter" in res.data, \
            "Task Presenter Editor should be an option"

        app = db.session.query(App).first()
        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        # As owner
        res = self.app.get('/app/sampleapp/tasks/', follow_redirects=True)
        assert res.status_code == 200, res.status_code
        assert "Edit the task presenter" in res.data, \
            "Task Presenter Editor should be an option"
        self.signout()
        # As anonymous
        res = self.app.get('/app/sampleapp/tasks/', follow_redirects=True)
        assert res.status_code == 401, res.status_code

        self.create()

        # As another user, but not owner
        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        res = self.app.get('/app/sampleapp/tasks/', follow_redirects=True)
        assert res.status_code == 403, res.status_code
        self.signout()

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_47_task_presenter_editor_loads(self, mock):
        """Test WEB task presenter editor loads"""
        self.register()
        self.new_application()
        res = self.app.get('/app/sampleapp/tasks/taskpresentereditor',
                           follow_redirects=True)
        err_msg = "Task Presenter options not found"
        assert "Task Presenter Editor" in res.data, err_msg
        err_msg = "Basic template not found"
        assert "The most basic template" in res.data, err_msg
        err_msg = "Image Pattern Recognition not found"
        assert "Flickr Person Finder template" in res.data, err_msg
        err_msg = "Geo-coding"
        assert "Urban Park template" in res.data, err_msg
        err_msg = "Transcribing documents"
        assert "PDF transcription template" in res.data, err_msg


    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_48_task_presenter_editor_works(self, mock):
        """Test WEB task presenter editor works"""
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        err_msg = "Task Presenter should be empty"
        assert not app.info.get('task_presenter'), err_msg

        res = self.app.get('/app/sampleapp/tasks/taskpresentereditor?template=basic',
                           follow_redirects=True)
        assert "var editor" in res.data, "CodeMirror Editor not found"
        assert "Task Presenter" in res.data, "CodeMirror Editor not found"
        assert "Task Presenter Preview" in res.data, "CodeMirror View not found"
        res = self.app.post('/app/sampleapp/tasks/taskpresentereditor',
                            data={'editor': 'Some HTML code!'},
                            follow_redirects=True)
        assert "Sample Project" in res.data, "Does not return to app details"
        app = db.session.query(App).first()
        err_msg = "Task Presenter failed to update"
        assert app.info['task_presenter'] == 'Some HTML code!', err_msg

        # Check it loads the previous posted code:
        res = self.app.get('/app/sampleapp/tasks/taskpresentereditor',
                           follow_redirects=True)
        assert "Some HTML code" in res.data, res.data

        # Now with hidden apps
        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        res = self.app.get('/app/sampleapp/tasks/taskpresentereditor?template=basic',
                           follow_redirects=True)
        assert "var editor" in res.data, "CodeMirror Editor not found"
        assert "Task Presenter" in res.data, "CodeMirror Editor not found"
        assert "Task Presenter Preview" in res.data, "CodeMirror View not found"

        res = self.app.post('/app/sampleapp/tasks/taskpresentereditor',
                            data={'editor': 'Some HTML code!'},
                            follow_redirects=True)
        assert "Sample Project" in res.data, "Does not return to app details"
        app = db.session.query(App).first()
        err_msg = "Task Presenter failed to update"
        assert app.info['task_presenter'] == 'Some HTML code!', err_msg

        # Check it loads the previous posted code:
        res = self.app.get('/app/sampleapp/tasks/taskpresentereditor',
                           follow_redirects=True)
        assert "Some HTML code" in res.data, res.data

        self.signout()
        self.create()
        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        res = self.app.get('/app/sampleapp/tasks/taskpresentereditor?template=basic',
                           follow_redirects=True)
        assert res.status_code == 403


    @patch('pybossa.ckan.requests.get')
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.forms.validator.requests.get')
    def test_48_update_app_info(self, Mock, mock, mock_webhook):
        """Test WEB project update/edit works keeping previous info values"""
        html_request = FakeRequest(json.dumps(self.pkg_json_not_found), 200,
                                   {'content-type': 'application/json'})
        Mock.return_value = html_request

        mock_webhook.return_value = html_request
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        err_msg = "Task Presenter should be empty"
        assert not app.info.get('task_presenter'), err_msg

        res = self.app.post('/app/sampleapp/tasks/taskpresentereditor',
                            data={'editor': 'Some HTML code!'},
                            follow_redirects=True)
        assert "Sample Project" in res.data, "Does not return to app details"
        app = db.session.query(App).first()
        for i in range(10):
            key = "key_%s" % i
            app.info[key] = i
        db.session.add(app)
        db.session.commit()
        _info = app.info

        self.update_application()
        app = db.session.query(App).first()
        for key in _info:
            assert key in app.info.keys(), \
                "The key %s is lost and it should be here" % key
        assert app.name == "Sample Project", "The project has not been updated"
        error_msg = "The project description has not been updated"
        assert app.description == "Description", error_msg
        error_msg = "The project long description has not been updated"
        assert app.long_description == "Long desc", error_msg

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_49_announcement_messages(self, mock):
        """Test WEB announcement messages works"""
        self.register()
        res = self.app.get("/", follow_redirects=True)
        error_msg = "There should be a message for the root user"
        print res.data
        assert "Root Message" in res.data, error_msg
        error_msg = "There should be a message for the user"
        assert "User Message" in res.data, error_msg
        error_msg = "There should not be an owner message"
        assert "Owner Message" not in res.data, error_msg
        # Now make the user a project owner
        self.new_application()
        res = self.app.get("/", follow_redirects=True)
        error_msg = "There should be a message for the root user"
        assert "Root Message" in res.data, error_msg
        error_msg = "There should be a message for the user"
        assert "User Message" in res.data, error_msg
        error_msg = "There should be an owner message"
        assert "Owner Message" in res.data, error_msg
        self.signout()

        # Register another user
        self.register(fullname="Jane Doe", name="janedoe",
                      password="janedoe", email="jane@jane.com")
        res = self.app.get("/", follow_redirects=True)
        error_msg = "There should not be a message for the root user"
        assert "Root Message" not in res.data, error_msg
        error_msg = "There should be a message for the user"
        assert "User Message" in res.data, error_msg
        error_msg = "There should not be an owner message"
        assert "Owner Message" not in res.data, error_msg
        self.signout()

        # Now as an anonymous user
        res = self.app.get("/", follow_redirects=True)
        error_msg = "There should not be a message for the root user"
        assert "Root Message" not in res.data, error_msg
        error_msg = "There should not be a message for the user"
        assert "User Message" not in res.data, error_msg
        error_msg = "There should not be an owner message"
        assert "Owner Message" not in res.data, error_msg

    @with_context
    def test_50_export_task_json(self):
        """Test WEB export Tasks to JSON works"""
        Fixtures.create()
        # First test for a non-existant app
        uri = '/app/somethingnotexists/tasks/export'
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Now get the tasks in JSON format
        uri = "/app/somethingnotexists/tasks/export?type=task&format=json"
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

        # Now with a real app
        uri = '/app/%s/tasks/export' % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        heading = "<strong>%s</strong>: Export All Tasks and Task Runs" % Fixtures.app_name
        assert heading in res.data, "Export page should be available\n %s" % res.data
        # Now test that a 404 is raised when an arg is invalid
        uri = "/app/%s/tasks/export?type=ask&format=json" % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        uri = "/app/%s/tasks/export?format=json" % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        uri = "/app/%s/tasks/export?type=task" % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # And a 415 is raised if the requested format is not supported or invalid
        uri = "/app/%s/tasks/export?type=task&format=gson" % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '415 UNSUPPORTED MEDIA TYPE', res.status

        # Now get the tasks in JSON format
        uri = "/app/%s/tasks/export?type=task&format=json" % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        exported_tasks = json.loads(res.data)
        app = db.session.query(App)\
                .filter_by(short_name=Fixtures.app_short_name)\
                .first()
        err_msg = "The number of exported tasks is different from App Tasks"
        assert len(exported_tasks) == len(app.tasks), err_msg
        # Tasks are exported as an attached file
        content_disposition = 'attachment; filename=test-app_task.json'
        assert res.headers.get('Content-Disposition') == content_disposition, res.headers

        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        res = self.app.get('app/%s/tasks/export' % (app.short_name),
                           follow_redirects=True)
        assert res.status_code == 401, res.status_code

        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        res = self.app.get('app/%s/tasks/export' % (app.short_name),
                           follow_redirects=True)
        assert res.status_code == 403, res.status_code
        # Owner
        self.signin(email=Fixtures.email_addr, password=Fixtures.password)
        res = self.app.get('app/%s/tasks/export' % (app.short_name),
                           follow_redirects=True)
        assert res.status_code == 200, res.status_code

    def test_export_task_json_support_non_latin1_project_names(self):
        app = AppFactory.create(name='Измени Киев!', short_name='Измени Киев!')
        res = self.app.get('app/%s/tasks/export?type=task&format=json' % app.short_name,
                           follow_redirects=True)
        assert 'Измени Киев!' in res.headers.get('Content-Disposition'), res

    def test_export_taskrun_json_support_non_latin1_project_names(self):
        app = AppFactory.create(name='Измени Киев!', short_name='Измени Киев!')
        res = self.app.get('app/%s/tasks/export?type=task_run&format=json' % app.short_name,
                           follow_redirects=True)
        assert 'Измени Киев!' in res.headers.get('Content-Disposition'), res

    def test_export_task_csv_support_non_latin1_project_names(self):
        app = AppFactory.create(name='Измени Киев!', short_name='Измени Киев!')
        TaskFactory.create(app=app)
        res = self.app.get('/app/%s/tasks/export?type=task&format=csv' % app.short_name,
                           follow_redirects=True)
        assert 'Измени Киев!' in res.headers.get('Content-Disposition'), res

    def test_export_taskrun_csv_support_non_latin1_project_names(self):
        app = AppFactory.create(name='Измени Киев!', short_name='Измени Киев!')
        task = TaskFactory.create(app=app)
        TaskRunFactory.create(task=task)
        res = self.app.get('/app/%s/tasks/export?type=task_run&format=csv' % app.short_name,
                           follow_redirects=True)
        assert 'Измени Киев!' in res.headers.get('Content-Disposition'), res

    @with_context
    def test_51_export_taskruns_json(self):
        """Test WEB export Task Runs to JSON works"""
        Fixtures.create()
        # First test for a non-existant app
        uri = '/app/somethingnotexists/tasks/export'
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Now get the tasks in JSON format
        uri = "/app/somethingnotexists/tasks/export?type=task&format=json"
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

        # Now with a real app
        uri = '/app/%s/tasks/export' % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        heading = "<strong>%s</strong>: Export All Tasks and Task Runs" % Fixtures.app_name
        assert heading in res.data, "Export page should be available\n %s" % res.data
        # Now get the tasks in JSON format
        uri = "/app/%s/tasks/export?type=task_run&format=json" % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        exported_task_runs = json.loads(res.data)
        app = db.session.query(App)\
                .filter_by(short_name=Fixtures.app_short_name)\
                .first()
        err_msg = "The number of exported task runs is different from App Tasks"
        assert len(exported_task_runs) == len(app.task_runs), err_msg
        # Task runs are exported as an attached file
        content_disposition = 'attachment; filename=test-app_task_run.json'
        assert res.headers.get('Content-Disposition') == content_disposition, res.headers

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_52_export_task_csv(self, mock):
        """Test WEB export Tasks to CSV works"""
        #Fixtures.create()
        # First test for a non-existant app
        uri = '/app/somethingnotexists/tasks/export'
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Now get the tasks in CSV format
        uri = "/app/somethingnotexists/tasks/export?type=task&format=csv"
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Now get the wrong table name in CSV format
        uri = "/app/%s/tasks/export?type=wrong&format=csv" % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

        # Now with a real app
        app = AppFactory.create()
        for i in range(0, 5):
            task = TaskFactory.create(app=app, info={'question': i})
        uri = '/app/%s/tasks/export' % app.short_name
        res = self.app.get(uri, follow_redirects=True)
        heading = "<strong>%s</strong>: Export All Tasks and Task Runs" % app.name
        assert heading in res.data, "Export page should be available\n %s" % res.data
        # Now get the tasks in CSV format
        uri = "/app/%s/tasks/export?type=task&format=csv" % app.short_name
        res = self.app.get(uri, follow_redirects=True)
        csv_content = StringIO.StringIO(res.data)
        csvreader = unicode_csv_reader(csv_content)
        app = db.session.query(App)\
                .filter_by(short_name=app.short_name)\
                .first()
        exported_tasks = []
        n = 0
        for row in csvreader:
            print row
            if n != 0:
                exported_tasks.append(row)
            else:
                keys = row
            n = n + 1
        err_msg = "The number of exported tasks is different from App Tasks"
        assert len(exported_tasks) == len(app.tasks), err_msg
        for t in app.tasks:
            err_msg = "All the task column names should be included"
            for tk in t.dictize().keys():
                expected_key = "task__%s" % tk
                assert expected_key in keys, err_msg
            err_msg = "All the task.info column names should be included"
            for tk in t.info.keys():
                expected_key = "taskinfo__%s" % tk
                assert expected_key in keys, err_msg

        for et in exported_tasks:
            task_id = et[keys.index('task__id')]
            task = db.session.query(Task).get(task_id)
            task_dict = task.dictize()
            for k in task_dict:
                slug = 'task__%s' % k
                err_msg = "%s != %s" % (task_dict[k], et[keys.index(slug)])
                if k != 'info':
                    assert unicode(task_dict[k]) == et[keys.index(slug)], err_msg
                else:
                    assert json.dumps(task_dict[k]) == et[keys.index(slug)], err_msg
            for k in task_dict['info'].keys():
                slug = 'taskinfo__%s' % k
                err_msg = "%s != %s" % (task_dict['info'][k], et[keys.index(slug)])
                assert unicode(task_dict['info'][k]) == et[keys.index(slug)], err_msg
        # Tasks are exported as an attached file
        content_disposition = 'attachment; filename=app1_task.csv'
        assert res.headers.get('Content-Disposition') == content_disposition, res.headers

        # With an empty app
        app = AppFactory.create()
        # Now get the tasks in CSV format
        uri = "/app/%s/tasks/export?type=task&format=csv" % app.short_name
        res = self.app.get(uri, follow_redirects=True)
        msg = "project does not have tasks"
        assert msg in res.data, msg

    @with_context
    def test_53_export_task_runs_csv(self):
        """Test WEB export Task Runs to CSV works"""
        # First test for a non-existant app
        uri = '/app/somethingnotexists/tasks/export'
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Now get the tasks in CSV format
        uri = "/app/somethingnotexists/tasks/export?type=tas&format=csv"
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

        # Now with a real app
        app = AppFactory.create()
        task = TaskFactory.create(app=app)
        for i in range(2):
            task_run = TaskRunFactory.create(app=app, task=task, info={'answer': i})
        uri = '/app/%s/tasks/export' % app.short_name
        res = self.app.get(uri, follow_redirects=True)
        heading = "<strong>%s</strong>: Export All Tasks and Task Runs" % app.name
        assert heading in res.data, "Export page should be available\n %s" % res.data
        # Now get the tasks in CSV format
        uri = "/app/%s/tasks/export?type=task_run&format=csv" % app.short_name
        res = self.app.get(uri, follow_redirects=True)
        csv_content = StringIO.StringIO(res.data)
        csvreader = unicode_csv_reader(csv_content)
        app = db.session.query(App)\
                .filter_by(short_name=app.short_name)\
                .first()
        exported_task_runs = []
        n = 0
        for row in csvreader:
            if n != 0:
                exported_task_runs.append(row)
            else:
                keys = row
            n = n + 1
        err_msg = "The number of exported task runs is different \
                   from App Tasks Runs: %s != %s" % (len(exported_task_runs), len(app.task_runs))
        assert len(exported_task_runs) == len(app.task_runs), err_msg

        for t in app.tasks[0].task_runs:
            for tk in t.dictize().keys():
                expected_key = "task_run__%s" % tk
                assert expected_key in keys, expected_key
            for tk in t.info.keys():
                expected_key = "task_runinfo__%s" % tk
                assert expected_key in keys, expected_key

        for et in exported_task_runs:
            task_run_id = et[keys.index('task_run__id')]
            task_run = db.session.query(TaskRun).get(task_run_id)
            task_run_dict = task_run.dictize()
            for k in task_run_dict:
                slug = 'task_run__%s' % k
                err_msg = "%s != %s" % (task_run_dict[k], et[keys.index(slug)])
                if k != 'info':
                    assert unicode(task_run_dict[k]) == et[keys.index(slug)], err_msg
                else:
                    assert json.dumps(task_run_dict[k]) == et[keys.index(slug)], err_msg
            for k in task_run_dict['info'].keys():
                slug = 'task_runinfo__%s' % k
                err_msg = "%s != %s" % (task_run_dict['info'][k], et[keys.index(slug)])
                assert unicode(task_run_dict['info'][k]) == et[keys.index(slug)], err_msg
        # Task runs are exported as an attached file
        content_disposition = 'attachment; filename=app1_task_run.csv'
        assert res.headers.get('Content-Disposition') == content_disposition, res.headers

    @with_context
    @patch('pybossa.view.applications.Ckan', autospec=True)
    def test_export_tasks_ckan_exception(self, mock1):
        mocks = [Mock()]
        from test_ckan import TestCkanModule
        fake_ckn = TestCkanModule()
        package = fake_ckn.pkg_json_found
        package['id'] = 3
        mocks[0].package_exists.return_value = (False,
                                                Exception("CKAN: error",
                                                          "error", 500))
        # mocks[0].package_create.return_value = fake_ckn.pkg_json_found
        # mocks[0].resource_create.return_value = dict(result=dict(id=3))
        # mocks[0].datastore_create.return_value = 'datastore'
        # mocks[0].datastore_upsert.return_value = 'datastore'

        mock1.side_effect = mocks

        """Test WEB Export CKAN Tasks works."""
        Fixtures.create()
        user = db.session.query(User).filter_by(name=Fixtures.name).first()
        app = db.session.query(App).first()
        user.ckan_api = 'ckan-api-key'
        app.owner_id = user.id
        db.session.add(user)
        db.session.add(app)
        db.session.commit()

        self.signin(email=user.email_addr, password=Fixtures.password)
        # Now with a real app
        uri = '/app/%s/tasks/export' % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        heading = "<strong>%s</strong>: Export All Tasks and Task Runs" % Fixtures.app_name
        assert heading in res.data, "Export page should be available\n %s" % res.data
        # Now get the tasks in CKAN format
        uri = "/app/%s/tasks/export?type=task&format=ckan" % Fixtures.app_short_name
        with patch.dict(self.flask_app.config, {'CKAN_URL': 'http://ckan.com'}):
            # First time exporting the package
            res = self.app.get(uri, follow_redirects=True)
            msg = 'Error'
            err_msg = "An exception should be raised"
            assert msg in res.data, err_msg

    @with_context
    @patch('pybossa.view.applications.Ckan', autospec=True)
    def test_export_tasks_ckan_connection_error(self, mock1):
        mocks = [Mock()]
        from test_ckan import TestCkanModule
        fake_ckn = TestCkanModule()
        package = fake_ckn.pkg_json_found
        package['id'] = 3
        mocks[0].package_exists.return_value = (False, ConnectionError)
        # mocks[0].package_create.return_value = fake_ckn.pkg_json_found
        # mocks[0].resource_create.return_value = dict(result=dict(id=3))
        # mocks[0].datastore_create.return_value = 'datastore'
        # mocks[0].datastore_upsert.return_value = 'datastore'

        mock1.side_effect = mocks

        """Test WEB Export CKAN Tasks works."""
        Fixtures.create()
        user = db.session.query(User).filter_by(name=Fixtures.name).first()
        app = db.session.query(App).first()
        user.ckan_api = 'ckan-api-key'
        app.owner_id = user.id
        db.session.add(user)
        db.session.add(app)
        db.session.commit()

        self.signin(email=user.email_addr, password=Fixtures.password)
        # Now with a real app
        uri = '/app/%s/tasks/export' % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        heading = "<strong>%s</strong>: Export All Tasks and Task Runs" % Fixtures.app_name
        assert heading in res.data, "Export page should be available\n %s" % res.data
        # Now get the tasks in CKAN format
        uri = "/app/%s/tasks/export?type=task&format=ckan" % Fixtures.app_short_name
        with patch.dict(self.flask_app.config, {'CKAN_URL': 'http://ckan.com'}):
            # First time exporting the package
            res = self.app.get(uri, follow_redirects=True)
            msg = 'CKAN server seems to be down'
            err_msg = "A connection exception should be raised"
            assert msg in res.data, err_msg

    @with_context
    @patch('pybossa.view.applications.Ckan', autospec=True)
    def test_task_export_tasks_ckan_first_time(self, mock1):
        """Test WEB Export CKAN Tasks works without an existing package."""
        # Second time exporting the package
        mocks = [Mock()]
        resource = dict(name='task', id=1)
        package = dict(id=3, resources=[resource])
        mocks[0].package_exists.return_value = (None, None)
        mocks[0].package_create.return_value = package
        #mocks[0].datastore_delete.return_value = None
        mocks[0].datastore_create.return_value = None
        mocks[0].datastore_upsert.return_value = None
        mocks[0].resource_create.return_value = dict(result=dict(id=3))
        mocks[0].datastore_create.return_value = 'datastore'
        mocks[0].datastore_upsert.return_value = 'datastore'

        mock1.side_effect = mocks

        Fixtures.create()
        user = db.session.query(User).filter_by(name=Fixtures.name).first()
        app = db.session.query(App).first()
        user.ckan_api = 'ckan-api-key'
        app.owner_id = user.id
        db.session.add(user)
        db.session.add(app)
        db.session.commit()

        self.signin(email=user.email_addr, password=Fixtures.password)
        # First test for a non-existant app
        uri = '/app/somethingnotexists/tasks/export'
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Now get the tasks in CKAN format
        uri = "/app/somethingnotexists/tasks/export?type=task&format=ckan"
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Now get the tasks in CKAN format
        uri = "/app/somethingnotexists/tasks/export?type=other&format=ckan"
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status


        # Now with a real app
        uri = '/app/%s/tasks/export' % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        heading = "<strong>%s</strong>: Export All Tasks and Task Runs" % Fixtures.app_name
        assert heading in res.data, "Export page should be available\n %s" % res.data
        # Now get the tasks in CKAN format
        uri = "/app/%s/tasks/export?type=task&format=ckan" % Fixtures.app_short_name
        #res = self.app.get(uri, follow_redirects=True)
        with patch.dict(self.flask_app.config, {'CKAN_URL': 'http://ckan.com'}):
            # First time exporting the package
            res = self.app.get(uri, follow_redirects=True)
            msg = 'Data exported to http://ckan.com'
            err_msg = "Tasks should be exported to CKAN"
            assert msg in res.data, err_msg



    @with_context
    @patch('pybossa.view.applications.Ckan', autospec=True)
    def test_task_export_tasks_ckan_second_time(self, mock1):
        """Test WEB Export CKAN Tasks works with an existing package."""
        # Second time exporting the package
        mocks = [Mock()]
        resource = dict(name='task', id=1)
        package = dict(id=3, resources=[resource])
        mocks[0].package_exists.return_value = (package, None)
        mocks[0].package_update.return_value = package
        mocks[0].datastore_delete.return_value = None
        mocks[0].datastore_create.return_value = None
        mocks[0].datastore_upsert.return_value = None
        mocks[0].resource_create.return_value = dict(result=dict(id=3))
        mocks[0].datastore_create.return_value = 'datastore'
        mocks[0].datastore_upsert.return_value = 'datastore'

        mock1.side_effect = mocks

        Fixtures.create()
        user = db.session.query(User).filter_by(name=Fixtures.name).first()
        app = db.session.query(App).first()
        user.ckan_api = 'ckan-api-key'
        app.owner_id = user.id
        db.session.add(user)
        db.session.add(app)
        db.session.commit()

        self.signin(email=user.email_addr, password=Fixtures.password)
        # First test for a non-existant app
        uri = '/app/somethingnotexists/tasks/export'
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Now get the tasks in CKAN format
        uri = "/app/somethingnotexists/tasks/export?type=task&format=ckan"
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

        # Now with a real app
        uri = '/app/%s/tasks/export' % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        heading = "<strong>%s</strong>: Export All Tasks and Task Runs" % Fixtures.app_name
        assert heading in res.data, "Export page should be available\n %s" % res.data
        # Now get the tasks in CKAN format
        uri = "/app/%s/tasks/export?type=task&format=ckan" % Fixtures.app_short_name
        #res = self.app.get(uri, follow_redirects=True)
        with patch.dict(self.flask_app.config, {'CKAN_URL': 'http://ckan.com'}):
            # First time exporting the package
            res = self.app.get(uri, follow_redirects=True)
            msg = 'Data exported to http://ckan.com'
            err_msg = "Tasks should be exported to CKAN"
            assert msg in res.data, err_msg

    @with_context
    @patch('pybossa.view.applications.Ckan', autospec=True)
    def test_task_export_tasks_ckan_without_resources(self, mock1):
        """Test WEB Export CKAN Tasks works without resources ."""
        mocks = [Mock()]
        package = dict(id=3, resources=[])
        mocks[0].package_exists.return_value = (package, None)
        mocks[0].package_update.return_value = package
        mocks[0].resource_create.return_value = dict(result=dict(id=3))
        mocks[0].datastore_create.return_value = 'datastore'
        mocks[0].datastore_upsert.return_value = 'datastore'


        mock1.side_effect = mocks

        Fixtures.create()
        user = db.session.query(User).filter_by(name=Fixtures.name).first()
        app = db.session.query(App).first()
        user.ckan_api = 'ckan-api-key'
        app.owner_id = user.id
        db.session.add(user)
        db.session.add(app)
        db.session.commit()

        self.signin(email=user.email_addr, password=Fixtures.password)
        # First test for a non-existant app
        uri = '/app/somethingnotexists/tasks/export'
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status
        # Now get the tasks in CKAN format
        uri = "/app/somethingnotexists/tasks/export?type=task&format=ckan"
        res = self.app.get(uri, follow_redirects=True)
        assert res.status == '404 NOT FOUND', res.status

        # Now with a real app
        uri = '/app/%s/tasks/export' % Fixtures.app_short_name
        res = self.app.get(uri, follow_redirects=True)
        heading = "<strong>%s</strong>: Export All Tasks and Task Runs" % Fixtures.app_name
        assert heading in res.data, "Export page should be available\n %s" % res.data
        # Now get the tasks in CKAN format
        uri = "/app/%s/tasks/export?type=task&format=ckan" % Fixtures.app_short_name
        #res = self.app.get(uri, follow_redirects=True)
        with patch.dict(self.flask_app.config, {'CKAN_URL': 'http://ckan.com'}):
            # First time exporting the package
            res = self.app.get(uri, follow_redirects=True)
            msg = 'Data exported to http://ckan.com'
            err_msg = "Tasks should be exported to CKAN"
            assert msg in res.data, err_msg

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_54_import_tasks(self, mock):
        """Test WEB import Task templates should work"""
        Fixtures.create()
        self.register()
        self.new_application()
        # Without tasks, there should be a template
        res = self.app.get('/app/sampleapp/tasks/import', follow_redirects=True)
        err_msg = "There should be a CSV template"
        assert "template=csv" in res.data, err_msg
        err_msg = "There should be an Image template"
        assert "mode=image" in res.data, err_msg
        err_msg = "There should be a Map template"
        assert "mode=map" in res.data, err_msg
        err_msg = "There should be a PDF template"
        assert "mode=pdf" in res.data, err_msg
        # With tasks
        self.new_task(1)
        res = self.app.get('/app/sampleapp/tasks/import', follow_redirects=True)
        err_msg = "There should load directly the basic template"
        err_msg = "There should not be a CSV template"
        assert "template=basic" not in res.data, err_msg
        err_msg = "There should not be an Image template"
        assert "template=image" not in res.data, err_msg
        err_msg = "There should not be a Map template"
        assert "template=map" not in res.data, err_msg
        err_msg = "There should not be a PDF template"
        assert "template=pdf" not in res.data, err_msg
        self.signout()

        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        res = self.app.get('/app/sampleapp/tasks/import', follow_redirects=True)
        assert res.status_code == 403, res.status_code

    @patch('pybossa.view.applications.redirect', wraps=redirect)
    @patch('pybossa.importers.requests.get')
    def test_import_tasks_redirects_on_success(self, request, redirect):
        """Test WEB when importing tasks succeeds, user is redirected to tasks main page"""
        csv_file = FakeRequest('Foo,Bar,Baz\n1,2,3', 200,
                                 {'content-type': 'text/plain'})
        request.return_value = csv_file
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % app.short_name
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'formtype': 'csv', 'form_name': 'csv'},
                            follow_redirects=True)

        assert "1 new task was imported successfully" in res.data
        redirect.assert_called_with('/app/%s/tasks/' % app.short_name)
        assert "Import Tasks" in res.data
        assert "Export Tasks" in res.data
        assert "Task Presenter" in res.data

    @patch('pybossa.view.applications.importers.create_importer_for')
    def test_import_few_tasks_is_done_synchronously(self, create_importer):
        """Test WEB importing a small amount of tasks is done synchronously"""
        importer = create_importer.return_value
        tasks_info = [{'info': {'Foo': i}} for i in range(1)]
        importer.tasks.return_value = tasks_info
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % app.short_name
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'formtype': 'csv', 'form_name': 'csv'},
                            follow_redirects=True)
        task = db.session.query(Task).first()

        assert task is not None, "Task was not imported"
        assert "1 new task was imported successfully" in res.data

    @patch('pybossa.view.applications.importer_queue', autospec=True)
    @patch('pybossa.view.applications.importers.create_importer_for')
    def test_import_tasks_as_background_job(self, create_importer, queue):
        """Test WEB importing a big amount of tasks is done in the background"""
        from pybossa.view.applications import MAX_NUM_SYNCHR_TASKS_IMPORT
        number_tasks = MAX_NUM_SYNCHR_TASKS_IMPORT + 1
        importer = create_importer.return_value
        tasks_info = [{'info': {'Foo': i}} for i in range(number_tasks)]
        importer.tasks.return_value = tasks_info
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % app.short_name
        res = self.app.post(url, data={'csv_url': 'http://myfakecsvurl.com',
                                       'formtype': 'csv', 'form_name': 'csv'},
                            follow_redirects=True)
        tasks = db.session.query(Task).all()

        assert tasks == [], "Tasks should not be immediately added"
        queue.enqueue.assert_called_once_with(import_tasks, tasks_info, app.id)
        msg = "You're trying to import a large amount of tasks, so please be patient.\
            You will receive an email when the tasks are ready."
        assert msg in res.data

    @with_context
    def test_55_facebook_account_warning(self):
        """Test WEB Facebook OAuth user gets a hint to sign in"""
        user = User(fullname='John',
                          name='john',
                          email_addr='john@john.com',
                          info={})

        user.info = dict(facebook_token=u'facebook')
        msg, method = get_user_signup_method(user)
        err_msg = "Should return 'facebook' but returned %s" % method
        assert method == 'facebook', err_msg

        user.info = dict(google_token=u'google')
        msg, method = get_user_signup_method(user)
        err_msg = "Should return 'google' but returned %s" % method
        assert method == 'google', err_msg

        user.info = dict(twitter_token=u'twitter')
        msg, method = get_user_signup_method(user)
        err_msg = "Should return 'twitter' but returned %s" % method
        assert method == 'twitter', err_msg

        user.info = {}
        msg, method = get_user_signup_method(user)
        err_msg = "Should return 'local' but returned %s" % method
        assert method == 'local', err_msg

    @with_context
    def test_56_delete_tasks(self):
        """Test WEB delete tasks works"""
        Fixtures.create()
        # Anonymous user
        res = self.app.get('/app/test-app/tasks/delete', follow_redirects=True)
        err_msg = "Anonymous user should be redirected for authentication"
        assert "Please sign in to access this page" in res.data, err_msg
        err_msg = "Anonymous user should not be allowed to delete tasks"
        res = self.app.post('/app/test-app/tasks/delete', follow_redirects=True)
        err_msg = "Anonymous user should not be allowed to delete tasks"
        assert "Please sign in to access this page" in res.data, err_msg

        # Authenticated user but not owner
        self.register()
        res = self.app.get('/app/test-app/tasks/delete', follow_redirects=True)
        err_msg = "Authenticated user but not owner should get 403 FORBIDDEN in GET"
        assert res.status == '403 FORBIDDEN', err_msg
        res = self.app.post('/app/test-app/tasks/delete', follow_redirects=True)
        err_msg = "Authenticated user but not owner should get 403 FORBIDDEN in POST"
        assert res.status == '403 FORBIDDEN', err_msg
        self.signout()

        # Owner
        tasks = db.session.query(Task).filter_by(app_id=1).all()
        res = self.signin(email=u'tester@tester.com', password=u'tester')
        res = self.app.get('/app/test-app/tasks/delete', follow_redirects=True)
        err_msg = "Owner user should get 200 in GET"
        assert res.status == '200 OK', err_msg
        assert len(tasks) > 0, "len(app.tasks) > 0"
        res = self.app.post('/app/test-app/tasks/delete', follow_redirects=True)
        err_msg = "Owner should get 200 in POST"
        assert res.status == '200 OK', err_msg
        tasks = db.session.query(Task).filter_by(app_id=1).all()
        assert len(tasks) == 0, "len(app.tasks) != 0"

        # Admin
        res = self.signin(email=u'root@root.com', password=u'tester' + 'root')
        res = self.app.get('/app/test-app/tasks/delete', follow_redirects=True)
        err_msg = "Admin user should get 200 in GET"
        assert res.status_code == 200, err_msg
        res = self.app.post('/app/test-app/tasks/delete', follow_redirects=True)
        err_msg = "Admin should get 200 in POST"
        assert res.status_code == 200, err_msg

    @with_context
    def test_57_reset_api_key(self):
        """Test WEB reset api key works"""
        url = "/account/johndoe/update"
        # Anonymous user
        res = self.app.get(url, follow_redirects=True)
        err_msg = "Anonymous user should be redirected for authentication"
        assert "Please sign in to access this page" in res.data, err_msg
        res = self.app.post(url, follow_redirects=True)
        assert "Please sign in to access this page" in res.data, err_msg
        # Authenticated user
        self.register()
        user = db.session.query(User).get(1)
        url = "/account/%s/update" % user.name
        api_key = user.api_key
        res = self.app.get(url, follow_redirects=True)
        err_msg = "Authenticated user should get access to reset api key page"
        assert res.status_code == 200, err_msg
        assert "reset your personal API Key" in res.data, err_msg
        url = "/account/%s/resetapikey" % user.name
        res = self.app.post(url, follow_redirects=True)
        err_msg = "Authenticated user should be able to reset his api key"
        assert res.status_code == 200, err_msg
        user = db.session.query(User).get(1)
        err_msg = "New generated API key should be different from old one"
        assert api_key != user.api_key, err_msg
        self.signout()

        self.register(fullname="new", name="new")
        res = self.app.post(url)
        assert res.status_code == 403, res.status_code

        url = "/account/fake/resetapikey"
        res = self.app.post(url)
        assert res.status_code == 404, res.status_code


    @with_context
    @patch('pybossa.cache.site_stats.get_locs', return_value=[{'latitude':0, 'longitude':0}])
    def test_58_global_stats(self, mock1):
        """Test WEB global stats of the site works"""
        Fixtures.create()

        url = "/stats"
        res = self.app.get(url, follow_redirects=True)
        err_msg = "There should be a Global Statistics page of the project"
        assert "General Statistics" in res.data, err_msg

        with patch.dict(self.flask_app.config, {'GEO': True}):
            res = self.app.get(url, follow_redirects=True)
            assert "GeoLite" in res.data, res.data

    @with_context
    def test_59_help_api(self):
        """Test WEB help api page exists"""
        Fixtures.create()
        url = "/help/api"
        res = self.app.get(url, follow_redirects=True)
        err_msg = "There should be a help api page"
        assert "API Help" in res.data, err_msg

    @with_context
    def test_59_help_license(self):
        """Test WEB help license page exists."""
        url = "/help/license"
        res = self.app.get(url, follow_redirects=True)
        err_msg = "There should be a help license page"
        assert "Licenses" in res.data, err_msg

    @with_context
    def test_59_about(self):
        """Test WEB help about page exists."""
        url = "/about"
        res = self.app.get(url, follow_redirects=True)
        err_msg = "There should be an about page"
        assert "About" in res.data, err_msg

    @with_context
    def test_59_help_tos(self):
        """Test WEB help TOS page exists."""
        url = "/help/terms-of-use"
        res = self.app.get(url, follow_redirects=True)
        err_msg = "There should be a TOS page"
        assert "Terms for use" in res.data, err_msg

    @with_context
    def test_59_help_policy(self):
        """Test WEB help policy page exists."""
        url = "/help/cookies-policy"
        res = self.app.get(url, follow_redirects=True)
        err_msg = "There should be a TOS page"
        assert "uses cookies" in res.data, err_msg

    @with_context
    def test_69_allow_anonymous_contributors(self):
        """Test WEB allow anonymous contributors works"""
        Fixtures.create()
        app = db.session.query(App).first()
        url = '/app/%s/newtask' % app.short_name

        # All users are allowed to participate by default
        # As Anonymous user
        res = self.app.get(url, follow_redirects=True)
        err_msg = "The anonymous user should be able to participate"
        assert app.name in res.data, err_msg

        # As registered user
        self.register()
        self.signin()
        res = self.app.get(url, follow_redirects=True)
        err_msg = "The anonymous user should be able to participate"
        assert app.name in res.data, err_msg
        self.signout()

        # Now only allow authenticated users
        app.allow_anonymous_contributors = False
        db.session.add(app)
        db.session.commit()

        # As Anonymous user
        res = self.app.get(url, follow_redirects=True)
        err_msg = "User should be redirected to sign in"
        app = db.session.query(App).first()
        msg = "Oops! You have to sign in to participate in <strong>%s</strong>" % app.name
        assert msg in res.data, err_msg

        # As registered user
        res = self.signin()
        res = self.app.get(url, follow_redirects=True)
        err_msg = "The authenticated user should be able to participate"
        assert app.name in res.data, err_msg
        self.signout()

        # However if the app is hidden, it should be forbidden
        app.hidden = 1
        db.session.add(app)
        db.session.commit()

        # As Anonymous user
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 401, res.status_code

        # As registered user
        self.signin()
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 403, res.status_code
        self.signout()

        # As admin
        self.signin(email=Fixtures.root_addr, password=Fixtures.root_password)
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 200, res.status_code
        self.signout()

        # As owner
        self.signin(email=Fixtures.email_addr, password=Fixtures.password)
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 200, res.status_code
        self.signout()

        # Now only allow authenticated users
        app.allow_anonymous_contributors = False
        app.hidden = 0
        db.session.add(app)
        db.session.commit()
        res = self.app.get(url, follow_redirects=True)
        err_msg = "Only authenticated users can participate"
        assert "You have to sign in" in res.data, err_msg


    @with_context
    def test_70_public_user_profile(self):
        """Test WEB public user profile works"""
        Fixtures.create()

        # Should work as an anonymous user
        url = '/account/%s/' % Fixtures.name
        res = self.app.get(url, follow_redirects=True)
        err_msg = "There should be a public profile page for the user"
        assert Fixtures.fullname in res.data, err_msg

        # Should work as an authenticated user
        self.signin()
        res = self.app.get(url, follow_redirects=True)
        assert Fixtures.fullname in res.data, err_msg

        # Should return 404 when a user does not exist
        url = '/account/a-fake-name-that-does-not-exist/'
        res = self.app.get(url, follow_redirects=True)
        err_msg = "It should return a 404"
        assert res.status_code == 404, err_msg

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.importers.requests.get')
    def test_71_bulk_epicollect_import_forbidden(self, Mock, mock):
        """Test WEB bulk import forbidden works"""
        unauthorized_request = FakeRequest('Forbidden', 403,
                                           {'content-type': 'application/json'})
        Mock.return_value = unauthorized_request
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'epicollect_project': 'fakeproject',
                                       'epicollect_form': 'fakeform',
                                       'formtype': 'json', 'form_name': 'epicollect'},
                            follow_redirects=True)
        msg = "Oops! It looks like you don't have permission to access the " \
              "EpiCollect Plus project"
        assert msg in res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.importers.requests.get')
    def test_72_bulk_epicollect_import_non_html(self, Mock, mock):
        """Test WEB bulk import non html works"""
        html_request = FakeRequest('Not an application/json', 200,
                                   {'content-type': 'text/html'})
        Mock.return_value = html_request
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        url = '/app/%s/tasks/import?template=csv' % (app.short_name)
        res = self.app.post(url, data={'epicollect_project': 'fakeproject',
                                       'epicollect_form': 'fakeform',
                                       'formtype': 'json', 'form_name': 'epicollect'},
                            follow_redirects=True)
        msg = "Oops! That project and form do not look like the right one."
        assert msg in res.data

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    @patch('pybossa.importers.requests.get')
    def test_73_bulk_epicollect_import_json(self, Mock, mock):
        """Test WEB bulk import json works"""
        data = [dict(DeviceID=23)]
        html_request = FakeRequest(json.dumps(data), 200,
                                   {'content-type': 'application/json'})
        Mock.return_value = html_request
        self.register()
        self.new_application()
        app = db.session.query(App).first()
        res = self.app.post(('/app/%s/tasks/import' % (app.short_name)),
                            data={'epicollect_project': 'fakeproject',
                                  'epicollect_form': 'fakeform',
                                  'formtype': 'json', 'form_name': 'epicollect'},
                            follow_redirects=True)

        app = db.session.query(App).first()
        err_msg = "Tasks should be imported"
        assert "1 new task was imported successfully" in res.data, err_msg
        tasks = db.session.query(Task).filter_by(app_id=app.id).all()
        err_msg = "The imported task from EpiCollect is wrong"
        assert tasks[0].info['DeviceID'] == 23, err_msg

        data = [dict(DeviceID=23), dict(DeviceID=24)]
        html_request = FakeRequest(json.dumps(data), 200,
                                   {'content-type': 'application/json'})
        Mock.return_value = html_request
        res = self.app.post(('/app/%s/tasks/import' % (app.short_name)),
                            data={'epicollect_project': 'fakeproject',
                                  'epicollect_form': 'fakeform',
                                  'formtype': 'json', 'form_name': 'epicollect'},
                            follow_redirects=True)
        app = db.session.query(App).first()
        assert len(app.tasks) == 2, "There should be only 2 tasks"
        n = 0
        epi_tasks = [{u'DeviceID': 23}, {u'DeviceID': 24}]
        for t in app.tasks:
            assert t.info == epi_tasks[n], "The task info should be the same"
            n += 1

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_74_task_settings_page(self, mock):
        """Test WEB TASK SETTINGS page works"""
        # Creat root user
        self.register()
        self.signout()
        # As owner
        self.register(fullname="owner", name="owner")
        res = self.new_application()
        url = "/app/sampleapp/tasks/settings"

        res = self.app.get(url, follow_redirects=True)
        dom = BeautifulSoup(res.data)
        divs = ['task_scheduler', 'task_delete', 'task_redundancy']
        for div in divs:
            err_msg = "There should be a %s section" % div
            assert dom.find(id=div) is not None, err_msg

        self.signout()
        # As an authenticated user
        self.register(fullname="juan", name="juan")
        res = self.app.get(url, follow_redirects=True)
        err_msg = "User should not be allowed to access this page"
        assert res.status_code == 403, err_msg
        self.signout()

        # As an anonymous user
        res = self.app.get(url, follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "User should be redirected to sign in"
        assert dom.find(id="signin") is not None, err_msg

        # As root
        self.signin()
        res = self.app.get(url, follow_redirects=True)
        dom = BeautifulSoup(res.data)
        divs = ['task_scheduler', 'task_delete', 'task_redundancy']
        for div in divs:
            err_msg = "There should be a %s section" % div
            assert dom.find(id=div) is not None, err_msg

    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_75_task_settings_scheduler(self, mock):
        """Test WEB TASK SETTINGS scheduler page works"""
        # Creat root user
        self.register()
        self.signout()
        # Create owner
        self.register(fullname="owner", name="owner")
        self.new_application()
        url = "/app/sampleapp/tasks/scheduler"
        form_id = 'task_scheduler'
        self.signout()

        # As owner and root
        for i in range(0, 1):
            if i == 0:
                # As owner
                self.signin(email="owner@example.com")
                sched = 'random'
            else:
                sched = 'default'
                self.signin()
            res = self.app.get(url, follow_redirects=True)
            dom = BeautifulSoup(res.data)
            err_msg = "There should be a %s section" % form_id
            assert dom.find(id=form_id) is not None, err_msg
            res = self.task_settings_scheduler(short_name="sampleapp",
                                               sched=sched)
            dom = BeautifulSoup(res.data)
            err_msg = "Task Scheduler should be updated"
            assert dom.find(id='msg_success') is not None, err_msg
            app = db.session.query(App).get(1)
            assert app.info['sched'] == sched, err_msg
            self.signout()

        # As an authenticated user
        self.register(fullname="juan", name="juan")
        res = self.app.get(url, follow_redirects=True)
        err_msg = "User should not be allowed to access this page"
        assert res.status_code == 403, err_msg
        self.signout()

        # As an anonymous user
        res = self.app.get(url, follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "User should be redirected to sign in"
        assert dom.find(id="signin") is not None, err_msg

        # With hidden app
        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        self.register(fullname="daniel", name="daniel")
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 403, res.status_code
        self.signout()
        self.signin()
        res = self.app.get(url, follow_redirects=True)
        dom = BeautifulSoup(res.data)
        # Correct values
        err_msg = "There should be a %s section" % form_id
        assert dom.find(id=form_id) is not None, err_msg


    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_76_task_settings_redundancy(self, mock):
        """Test WEB TASK SETTINGS redundancy page works"""
        # Creat root user
        self.register()
        self.signout()
        # Create owner
        self.register(fullname="owner", name="owner")
        self.new_application()
        self.new_task(1)

        url = "/app/sampleapp/tasks/redundancy"
        form_id = 'task_redundancy'
        self.signout()

        # As owner and root
        for i in range(0, 1):
            if i == 0:
                # As owner
                self.signin(email="owner@example.com")
                n_answers = 20
            else:
                n_answers = 10
                self.signin()
            res = self.app.get(url, follow_redirects=True)
            dom = BeautifulSoup(res.data)
            # Correct values
            err_msg = "There should be a %s section" % form_id
            assert dom.find(id=form_id) is not None, err_msg
            res = self.task_settings_redundancy(short_name="sampleapp",
                                                n_answers=n_answers)
            db.session.close()
            dom = BeautifulSoup(res.data)
            err_msg = "Task Redundancy should be updated"
            assert dom.find(id='msg_success') is not None, err_msg
            app = db.session.query(App).get(1)
            for t in app.tasks:
                assert t.n_answers == n_answers, err_msg
            # Wrong values, triggering the validators
            res = self.task_settings_redundancy(short_name="sampleapp",
                                                n_answers=0)
            dom = BeautifulSoup(res.data)
            err_msg = "Task Redundancy should be a value between 0 and 1000"
            assert dom.find(id='msg_error') is not None, err_msg
            res = self.task_settings_redundancy(short_name="sampleapp",
                                                n_answers=10000000)
            dom = BeautifulSoup(res.data)
            err_msg = "Task Redundancy should be a value between 0 and 1000"
            assert dom.find(id='msg_error') is not None, err_msg

            self.signout()

        # As an authenticated user
        self.register(fullname="juan", name="juan")
        res = self.app.get(url, follow_redirects=True)
        err_msg = "User should not be allowed to access this page"
        assert res.status_code == 403, err_msg
        self.signout()

        # As an anonymous user
        res = self.app.get(url, follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "User should be redirected to sign in"
        assert dom.find(id="signin") is not None, err_msg

        # With hidden app
        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        self.register(fullname="daniel", name="daniel")
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 403, res.status_code
        self.signout()
        self.signin()
        res = self.app.get(url, follow_redirects=True)
        dom = BeautifulSoup(res.data)
        # Correct values
        err_msg = "There should be a %s section" % form_id
        assert dom.find(id=form_id) is not None, err_msg

    @with_context
    def test_task_redundancy_update_updates_task_state(self):
        """Test WEB when updating the redundancy of the tasks in a project, the
        state of the task is updated in consecuence"""
        # Creat root user
        self.register()
        self.new_application()
        self.new_task(1)

        url = "/app/sampleapp/tasks/redundancy"

        app = db.session.query(App).get(1)
        for t in app.tasks:
            tr = TaskRun(app_id=app.id, task_id=t.id)
            db.session.add(tr)
            db.session.commit()

        err_msg = "Task state should be completed"
        res = self.task_settings_redundancy(short_name="sampleapp",
                                            n_answers=1)

        for t in app.tasks:
            assert t.state == 'completed', err_msg

        res = self.task_settings_redundancy(short_name="sampleapp",
                                            n_answers=2)
        err_msg = "Task state should be ongoing"
        db.session.add(app)
        db.session.commit()

        for t in app.tasks:
            assert t.state == 'ongoing', t.state


    @with_context
    @patch('pybossa.view.applications.uploader.upload_file', return_value=True)
    def test_77_task_settings_priority(self, mock):
        """Test WEB TASK SETTINGS priority page works"""
        # Creat root user
        self.register()
        self.signout()
        # Create owner
        self.register(fullname="owner", name="owner")
        self.new_application()
        self.new_task(1)
        url = "/app/sampleapp/tasks/priority"
        form_id = 'task_priority'
        self.signout()

        # As owner and root
        app = db.session.query(App).get(1)
        _id = app.tasks[0].id
        for i in range(0, 1):
            if i == 0:
                # As owner
                self.signin(email="owner@example.com")
                task_ids = str(_id)
                priority_0 = 1.0
            else:
                task_ids = "1"
                priority_0 = 0.5
                self.signin()
            res = self.app.get(url, follow_redirects=True)
            dom = BeautifulSoup(res.data)
            # Correct values
            err_msg = "There should be a %s section" % form_id
            assert dom.find(id=form_id) is not None, err_msg
            res = self.task_settings_priority(short_name="sampleapp",
                                              task_ids=task_ids,
                                              priority_0=priority_0)
            dom = BeautifulSoup(res.data)
            err_msg = "Task Priority should be updated"
            assert dom.find(id='msg_success') is not None, err_msg
            task = db.session.query(Task).get(_id)
            assert task.id == int(task_ids), err_msg
            assert task.priority_0 == priority_0, err_msg
            # Wrong values, triggering the validators
            res = self.task_settings_priority(short_name="sampleapp",
                                              priority_0=3,
                                              task_ids="1")
            dom = BeautifulSoup(res.data)
            err_msg = "Task Priority should be a value between 0.0 and 1.0"
            assert dom.find(id='msg_error') is not None, err_msg
            res = self.task_settings_priority(short_name="sampleapp",
                                              task_ids="1, 2")
            dom = BeautifulSoup(res.data)
            err_msg = "Task Priority task_ids should be a comma separated, no spaces, integers"
            assert dom.find(id='msg_error') is not None, err_msg
            res = self.task_settings_priority(short_name="sampleapp",
                                              task_ids="1,a")
            dom = BeautifulSoup(res.data)
            err_msg = "Task Priority task_ids should be a comma separated, no spaces, integers"
            assert dom.find(id='msg_error') is not None, err_msg

            self.signout()

        # As an authenticated user
        self.register(fullname="juan", name="juan")
        res = self.app.get(url, follow_redirects=True)
        err_msg = "User should not be allowed to access this page"
        assert res.status_code == 403, err_msg
        self.signout()

        # As an anonymous user
        res = self.app.get(url, follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "User should be redirected to sign in"
        assert dom.find(id="signin") is not None, err_msg

        # With hidden app
        app.hidden = 1
        db.session.add(app)
        db.session.commit()
        self.register(fullname="daniel", name="daniel")
        res = self.app.get(url, follow_redirects=True)
        assert res.status_code == 403, res.status_code
        self.signout()
        self.signin()
        res = self.app.get(url, follow_redirects=True)
        dom = BeautifulSoup(res.data)
        # Correct values
        err_msg = "There should be a %s section" % form_id
        assert dom.find(id=form_id) is not None, err_msg


    @with_context
    def test_78_cookies_warning(self):
        """Test WEB cookies warning is displayed"""
        # As Anonymous
        res = self.app.get('/', follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "If cookies are not accepted, cookies banner should be shown"
        assert dom.find(id='cookies_warning') is not None, err_msg

        # As user
        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        res = self.app.get('/', follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "If cookies are not accepted, cookies banner should be shown"
        assert dom.find(id='cookies_warning') is not None, err_msg
        self.signout()

        # As admin
        self.signin(email=Fixtures.root_addr, password=Fixtures.root_password)
        res = self.app.get('/', follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "If cookies are not accepted, cookies banner should be shown"
        assert dom.find(id='cookies_warning') is not None, err_msg
        self.signout()

    @with_context
    def test_79_cookies_warning2(self):
        """Test WEB cookies warning is hidden"""
        # As Anonymous
        self.app.set_cookie("localhost", "PyBossa_accept_cookies", "Yes")
        res = self.app.get('/', follow_redirects=True, headers={})
        dom = BeautifulSoup(res.data)
        err_msg = "If cookies are not accepted, cookies banner should be hidden"
        assert dom.find(id='cookies_warning') is None, err_msg

        # As user
        self.signin(email=Fixtures.email_addr2, password=Fixtures.password)
        res = self.app.get('/', follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "If cookies are not accepted, cookies banner should be hidden"
        assert dom.find(id='cookies_warning') is None, err_msg
        self.signout()

        # As admin
        self.signin(email=Fixtures.root_addr, password=Fixtures.root_password)
        res = self.app.get('/', follow_redirects=True)
        dom = BeautifulSoup(res.data)
        err_msg = "If cookies are not accepted, cookies banner should be hidden"
        assert dom.find(id='cookies_warning') is None, err_msg
        self.signout()


    @with_context
    def test_user_with_no_more_tasks_find_volunteers(self):
        """Test WEB when a user has contributed to all available tasks, he is
        asked to find new volunteers for a project, if the project is not
        completed yet (overall progress < 100%)"""

        self.register()
        user = User.query.first()
        app = AppFactory.create(owner=user)
        task = TaskFactory.create(app=app)
        taskrun = TaskRunFactory.create(task=task, user=user)
        res = self.app.get('/app/%s/newtask' % app.short_name)

        message = "Sorry, you've contributed to all the tasks for this project, but this project still needs more volunteers, so please spread the word!"
        assert message in res.data
        self.signout()


    @with_context
    def test_user_with_no_more_tasks_find_volunteers_project_completed(self):
        """Test WEB when a user has contributed to all available tasks, he is
        not asked to find new volunteers for a project, if the project is
        completed (overall progress = 100%)"""

        self.register()
        user = User.query.first()
        app = AppFactory.create(owner=user)
        task = TaskFactory.create(app=app, n_answers=1)
        taskrun = TaskRunFactory.create(task=task, user=user)
        res = self.app.get('/app/%s/newtask' % app.short_name)

        assert task.state == 'completed', task.state
        message = "Sorry, you've contributed to all the tasks for this project, but this project still needs more volunteers, so please spread the word!"
        assert message not in res.data
        self.signout()
