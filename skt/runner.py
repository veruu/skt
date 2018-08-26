# Copyright (c) 2017 Red Hat, Inc. All rights reserved. This copyrighted
# material is made available to anyone wishing to use, modify, copy, or
# redistribute it subject to the terms and conditions of the GNU General
# Public License v.2 or later.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

import logging
import os
import platform
import re
import subprocess
import time
import xml.etree.ElementTree as etree

import requests

from skt.misc import SKT_SUCCESS, SKT_FAIL, SKT_ERROR


class Runner(object):
    """An abstract test runner"""
    # TODO This probably shouldn't be here as we never use it, and it should
    # not be inherited
    TYPE = 'default'

    # TODO Define abstract "run" method.


class BeakerRunner(Runner):
    """Beaker test runner"""
    TYPE = 'beaker'

    def __init__(self, jobtemplate, jobowner=None, blacklist=None):
        """
        Initialize a runner executing tests on Beaker.

        Args:
            jobtemplate:    Path to a Beaker job template. Can contain a tilde
                            expression ('~' or '~user') to be expanded into
                            the current user's home directory.
            jobowner:       Name of a Beaker user on whose behalf the job
                            should be submitted, or None, if the owner should
                            be the current user.
            blacklist:      Path to file containing hostnames to blacklist from
                            running on, one hostname per line.
        """
        # Beaker job template file path
        # FIXME Move expansion up the call stack, as this limits the class
        # usefulness, because tilde is a valid path character.
        self.template = os.path.expanduser(jobtemplate)
        # Name of a Beaker user on whose behalf the job should be submitted,
        # or None, if the owner should be the current user.
        self.jobowner = jobowner
        self.blacklisted = self.__load_blacklist(blacklist)
        # Delay between checks of Beaker job statuses, seconds
        self.watchdelay = 60
        # Set of recipe sets that didn't complete yet
        self.watchlist = set()
        self.whiteboard = ''
        self.job_to_recipe_set_map = {}
        self.recipe_set_results = {}
        # Keep a set of completed recipes per set so we don't check them again
        self.completed_recipes = {}
        self.aborted_count = 0
        # Set up the default, allowing for overrides with each run
        self.max_aborted = 3

        logging.info("runner type: %s", self.TYPE)
        logging.info("beaker template: %s", self.template)

    def __load_blacklist(self, filepath):
        hostnames = []

        try:
            with open(filepath, 'r') as fileh:
                for line in fileh:
                    line = line.strip()
                    if line:
                        hostnames.append(line)
        except (IOError, OSError) as exc:
            logging.error('Can\'t access {}!'.format(filepath))
            raise exc
        except TypeError:
            logging.info('No hostname blacklist file passed')

        logging.info('Blacklisted hostnames: {}'.format(hostnames))
        return hostnames

    def __getxml(self, replacements):
        """
        Generate job XML with template replacements applied. Search the
        template for words surrounded by "##" strings and replace them with
        strings from the supplied dictionary.

        Args:
            replacements:   A dictionary of placeholder strings with "##"
                            around them, and their replacements.

        Returns:
            The job XML text with template replacements applied.
        """
        xml = ''
        with open(self.template, 'r') as fileh:
            for line in fileh:
                for match in re.finditer(r"##(\w+)##", line):
                    if match.group(1) in replacements:
                        line = line.replace(match.group(0),
                                            replacements[match.group(1)])

                xml += line

        return xml

    def getresultstree(self, taskspec):
        """
        Retrieve Beaker results for taskspec in Beaker's native XML format.

        Args:
            taskspec:   ID of the job, recipe or recipe set.

        Returns:
            etree node representing the results.
        """
        args = ["bkr", "job-results", taskspec]

        bkr = subprocess.Popen(args, stdout=subprocess.PIPE)
        (stdout, _) = bkr.communicate()
        return etree.fromstring(stdout)

    def __forget_taskspec(self, taskspec):
        """
        Remove a job or recipe set from self.job_to_recipe_set_map, and recipe
        set from self.watchlist if applicable.

        Args:
            taskspec: The job (J:xxxxx) or recipe set (RS:xxxxx) ID.
        """
        if taskspec.startswith("J:"):
            del self.job_to_recipe_set_map[taskspec]
        elif taskspec.startswith("RS:"):
            self.watchlist.discard(taskspec)
            deljids = set()
            for (jid, rset) in self.job_to_recipe_set_map.iteritems():
                if taskspec in rset:
                    rset.remove(taskspec)
                    if not rset:
                        deljids.add(jid)
            for jid in deljids:
                del self.job_to_recipe_set_map[jid]
        else:
            raise ValueError("Unknown taskspec type: %s" % taskspec)

    def __getresults(self):
        """
        Get return code based on the job results.

        Returns:
            SKT_SUCCESS if all jobs passed,
            SKT_FAIL in case of failures, and
            SKT_ERROR in case of infrastructure failures.
        """
        if not self.job_to_recipe_set_map:
            # We forgot every job / recipe set
            logging.error('All test sets aborted or were cancelled!')
            return SKT_ERROR

        for job, recipe_sets in self.job_to_recipe_set_map.items():
            for recipe_set_id in recipe_sets:
                results = self.recipe_set_results[recipe_set_id]
                for recipe_result in results.findall('recipe'):
                    if recipe_result.attrib.get('result') != 'Pass':
                        logging.info('Failure in a recipe detected!')
                        return SKT_FAIL

        logging.info('Testing passed!')
        return SKT_SUCCESS

    def __blacklist_hreq(self, host_requires):
        """
        Make sure recipe excludes blacklisted hosts.

        Args:
            host_requires: etree node representing "hostRequires" node from the
                           recipe.

        Returns:
            Modified "hostRequires" etree node.
        """
        and_node = host_requires.find('and')
        if and_node is None:
            and_node = etree.Element('and')
            host_requires.append(and_node)

        for disabled in self.blacklisted:
            hostname = etree.Element('hostname')
            hostname.set('op', '!=')
            hostname.set('value', disabled)
            and_node.append(hostname)

        return host_requires

    def __recipe_set_to_job(self, recipe_set, samehost=False):
        tmp = recipe_set.copy()

        for recipe in tmp.findall('recipe'):
            hreq = recipe.find("hostRequires")
            hostname = hreq.find('hostname')
            if hostname is not None:
                hreq.remove(hostname)
            if samehost:
                hostname = etree.Element("hostname")
                hostname.set("op", "=")
                hostname.set("value", recipe.attrib.get("system"))
                hreq.append(hostname)
            else:
                new_hreq = self.__blacklist_hreq(hreq)
                recipe.remove(hreq)
                recipe.append(new_hreq)

        newwb = etree.Element("whiteboard")
        newwb.text = "%s [RS:%s]" % (self.whiteboard, tmp.attrib.get("id"))

        newroot = etree.Element("job")
        newroot.append(newwb)
        newroot.append(tmp)

        return newroot

    def cancel_pending_jobs(self):
        """
        Cancel all recipe sets from self.watchlist and remove their IDs from
        self.job_to_recipe_set_map.
        """
        sets_to_cancel = [recipe_set for recipe_set in self.watchlist.copy()]
        if sets_to_cancel:
            ret = subprocess.call(['bkr', 'job-cancel'] + sets_to_cancel)
            if ret:
                logging.info('Failed to cancel the remaining recipe sets!')

        for job_id in self.job_to_recipe_set_map.keys():
            self.__forget_taskspec(job_id)

    def __watchloop(self):
        while self.watchlist:
            time.sleep(self.watchdelay)

            if self.max_aborted == self.aborted_count:
                # Remove / cancel all the remaining recipe set IDs and abort
                self.cancel_pending_jobs()

            for recipe_set_id in self.watchlist.copy():
                root = self.getresultstree(recipe_set_id)
                recipes = root.findall('recipe')

                for recipe in recipes:
                    result = recipe.attrib.get('result')
                    status = recipe.attrib.get('status')
                    recipe_id = 'R:' + recipe.attrib.get('id')
                    if status not in ['Completed', 'Aborted', 'Cancelled'] or \
                            recipe_id in self.completed_recipes[recipe_set_id]:
                        continue

                    logging.info("%s status changed to %s", recipe_id, status)
                    self.completed_recipes[recipe_set_id].add(recipe_id)
                    if len(self.completed_recipes[recipe_set_id]) == \
                            len(recipes):
                        self.watchlist.remove(recipe_set_id)
                        self.recipe_set_results[recipe_set_id] = root

                    if result == 'Pass':
                        continue

                    if status == 'Cancelled':
                        logging.error('Cancelled run detected! Cancelling the '
                                      'rest of runs and aborting!')
                        self.cancel_pending_jobs()
                        return

                    if result == 'Warn' and status == 'Aborted':
                        logging.warning('%s from %s aborted!',
                                        recipe_id,
                                        recipe_set_id)
                        self.__forget_taskspec(recipe_set_id)
                        self.aborted_count += 1

                        if self.aborted_count < self.max_aborted:
                            logging.warning('Resubmitting aborted %s',
                                            recipe_set_id)
                            newjob = self.__recipe_set_to_job(root)
                            newjobid = self.__jobsubmit(etree.tostring(newjob))
                            self.__add_to_watchlist(newjobid)
                        continue

                    # Something in the recipe set really reported failure
                    test_failure = False
                    for task in recipe.findall('task'):
                        if 'kpkginstall' in task.attrib.get('name', ''):
                            if task.attrib.get('result') in ['Pass', 'Panic']:
                                test_failure = True
                            else:
                                break
                        elif task.attrib.get('result') != 'Pass':
                            break

                    if not test_failure:
                        # Recipe failed before the tested kernel was installed
                        self.__forget_taskspec(recipe_set_id)
                        self.aborted_count += 1

                        if self.aborted_count < self.max_aborted:
                            logging.warning('Infrastructure-related problem '
                                            'found, resubmitting %s',
                                            recipe_set_id)
                            newjob = self.__recipe_set_to_job(root)
                            newjobid = self.__jobsubmit(etree.tostring(newjob))
                            self.__add_to_watchlist(newjobid)

    def __add_to_watchlist(self, jobid):
        root = self.getresultstree(jobid)

        if not self.whiteboard:
            self.whiteboard = root.find("whiteboard").text

        self.job_to_recipe_set_map[jobid] = set()
        for recipe_set in root.findall("recipeSet"):
            set_id = "RS:%s" % recipe_set.attrib.get("id")
            self.job_to_recipe_set_map[jobid].add(set_id)
            self.watchlist.add(set_id)
            self.completed_recipes[set_id] = set()
            logging.info("added %s to watchlist", set_id)

    def wait(self, jobid):
        self.__add_to_watchlist(jobid)
        self.__watchloop()

    def get_recipe_test_list(self, recipe_node):
        """
        Retrieve the list of tests which ran for a particular recipe. All tasks
        after kpkginstall are interpreted as ran tests.

        Args:
            recipe_node: ElementTree node representing the recipe, extracted
                         from Beaker XML or result XML.

        Returns:
            List of test names that ran.
        """
        test_list = []
        after_kpkg = False

        for test_task in recipe_node.findall('task'):
            if after_kpkg:
                test_list.append(test_task.attrib.get('name'))

            if 'kpkginstall' in test_task.attrib.get('name', ''):
                after_kpkg = True

        return test_list

    def __jobsubmit(self, xml):
        jobid = None
        args = ["bkr", "job-submit"]

        if self.jobowner is not None:
            args += ["--job-owner=%s" % self.jobowner]

        args += ["-"]

        bkr = subprocess.Popen(args, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE)

        (stdout, _) = bkr.communicate(xml)

        for line in stdout.split("\n"):
            m = re.match(r"^Submitted: \['([^']+)'\]$", line)
            if m:
                jobid = m.group(1)
                break

        if not jobid:
            raise Exception('Unable to submit the job!')

        logging.info("submitted jobid: %s", jobid)

        return jobid

    def run(self, url, max_aborted, release, wait=False,
            arch=platform.machine()):
        """
        Run tests in Beaker.

        Args:
            url:         URL pointing to kernel tarball.
            max_aborted: Maximum number of allowed aborted jobs. Abort the
                         whole stage if the number is reached.
            release:     NVR of the tested kernel.
            wait:        False if skt should exit after submitting the jobs,
                         True if it should wait for them to finish.
            arch:        Architecture of the machine the tests should run on,
                         in a format accepted by Beaker. Defaults to
                         architecture of the current machine skt is running on
                         if not specified.

        Returns:
            Tuple (ret, report_string) where ret can be
                   SKT_SUCCESS if everything passed
                   SKT_FAIL if testing failed
                   SKT_ERROR in case of infrastructure error (exceptions are
                                                              logged)
            and report_string is a string describing tests and results.
        """
        ret = SKT_SUCCESS
        report_string = ''
        self.watchlist = set()
        self.job_to_recipe_set_map = {}
        self.recipe_set_results = {}
        self.completed_recipes = {}
        self.aborted_count = 0
        self.max_aborted = max_aborted

        try:
            job_xml_tree = etree.fromstring(self.__getxml(
                {'KVER': release,
                 'KPKG_URL': url,
                 'ARCH': arch}
            ))
            for recipe in job_xml_tree.findall('recipeSet/recipe'):
                hreq = recipe.find('hostRequires')
                new_hreq = self.__blacklist_hreq(hreq)
                recipe.remove(hreq)
                recipe.append(new_hreq)

            jobid = self.__jobsubmit(etree.tostring(job_xml_tree))

            if wait:
                self.wait(jobid)
                ret = self.__getresults()
        except Exception as exc:
            logging.error(exc)
            ret = SKT_ERROR

        if ret == SKT_ERROR:
            return (ret, report_string)
        if not wait:
            return (ret, '\nSuccessfully submitted test job!')

        recipe_set_ids = set.union(*self.job_to_recipe_set_map.values())
        for recipe_set_id in recipe_set_ids:
            recipe_set_result = self.recipe_set_results[recipe_set_id]
            for recipe in recipe_set_result.findall('recipe'):
                failed_tasks = []
                recipe_result = recipe.attrib.get('result')

                report_string += '\n\n{} R:{} ({} arch): {}\n\n'.format(
                    'Test result for recipe',
                    recipe.attrib.get('id'),
                    recipe.find('hostRequires/and/arch').attrib.get('value'),
                    recipe_result.upper()
                )

                kpkginstall_task = recipe.find(
                    "task[@name='/distribution/kpkginstall']"
                )
                if kpkginstall_task.attrib.get('result') != 'Pass':
                    report_string += 'Kernel failed to boot!\n\n'
                    failed_tasks.append('/distribution/kpkginstall')
                else:
                    recipe_tests = self.get_recipe_test_list(recipe)
                    report_string += 'We ran the following tests:\n'
                    for test_name in recipe_tests:
                        test_result = recipe.find(
                            "task[@name='{}']".format(test_name)
                        ).attrib.get('result')
                        report_string += '  - {}: {}\n'.format(
                            test_name, test_result.upper()
                        )
                        if test_result != 'Pass':
                            failed_tasks.append(test_name)

                if failed_tasks:
                    report_string += '\n{}\n{}\n'.format(
                        'For more information about the failures, here are '
                        'links for the logs of',
                        'failed tests and their subtasks:'
                    )
                for failed_task in failed_tasks:
                    task_node = recipe.find(
                        "task[@name='{}']".format(failed_task)
                    )
                    report_string += '- {}\n'.format(failed_task)
                    for log in task_node.findall('logs/log'):
                        report_string += '  {}\n'.format(
                            log.attrib.get('href')
                        )
                    for subtask_log in task_node.findall(
                            'results/result/logs/log'
                    ):
                        report_string += '  {}\n'.format(
                            subtask_log.attrib.get('href')
                        )
                    report_string += '\n'

                machinedesc_url = recipe.find(
                    "task[@name='/test/misc/machineinfo']/logs/"
                    "log[@name='machinedesc.log']"
                ).attrib.get('href')
                machinedesc = requests.get(machinedesc_url).text
                report_string += '\n{}\n\n{}\n\n'.format(
                    'Testing was performed on a machine with following '
                    'parameters:',
                    machinedesc
                )

        return (ret, report_string)


def getrunner(rtype, rarg):
    """
    Create an instance of a "runner" subclass with specified arguments.

    Args:
        rtype:  The value of the class "TYPE" member to match.
        rarg:   A dictionary with the instance creation arguments.

    Returns:
        The created class instance.

    Raises:
        ValueError if the rtype match wasn't found.
    """
    for cls in Runner.__subclasses__():
        if cls.TYPE == rtype:
            return cls(**rarg)
    raise ValueError("Unknown runner type: %s" % rtype)
