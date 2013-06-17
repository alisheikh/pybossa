# This file is part of PyBOSSA.
#
# PyBOSSA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PyBOSSA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with PyBOSSA.  If not, see <http://www.gnu.org/licenses/>.

from flask import url_for


class Hateoas(object):
    def link(self, rel, title, href):
        return "<link rel='%s' title='%s' href='%s'/>" % (rel, title, href)

    def create_link(self, item, rel='self'):
        title = item.__class__.__name__.lower()
        method = ".api_%s" % title
        href = url_for(method, id=item.id, _external=True)
        return self.link(rel, title, href)

    def create_links(self, item):
        cls = item.__class__.__name__.lower()
        if cls == 'taskrun':
            link = self.create_link(item)
            links = []
            if item.project_id is not None:
                links.append(self.create_link(item.project, rel='parent'))
            if item.task_id is not None:
                links.append(self.create_link(item.task, rel='parent'))
            return links, link
        elif cls == 'task':
            link = self.create_link(item)
            links = []
            if item.project_id is not None:
                links = [self.create_link(item.project, rel='parent')]
            return links, link
        elif cls == 'project':
            return None, self.create_link(item)
        else:
            return False

    def remove_links(self, item):
        """Remove HATEOAS link and links from item"""
        if item.get('link'):
            item.pop('link')
        if item.get('links'):
            item.pop('links')
        return item