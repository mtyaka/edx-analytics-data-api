"""
Luigi tasks for extracting problem answer distribution statistics from
tracking log files.
"""
import csv
import hashlib
import html5lib
import json
from operator import itemgetter

import luigi
import luigi.hdfs
import luigi.s3

import edx.analytics.tasks.util.eventlog as eventlog
from edx.analytics.tasks.mapreduce import MapReduceJobTask, MultiOutputMapReduceJobTask
from edx.analytics.tasks.pathutil import PathSetTask
from edx.analytics.tasks.url import ExternalURL, IgnoredTarget
from edx.analytics.tasks.url import get_target_from_url, url_path_join

import logging
log = logging.getLogger(__name__)


################################
# Task Map-Reduce definitions
################################

UNKNOWN_ANSWER_VALUE = ''
UNMAPPED_ANSWER_VALUE = ''


class LastProblemCheckEventMixin(object):
    """Identifies last problem_check event for a user on a problem in a course, given raw event log input."""

    def mapper(self, line):
        """
        Generates output values for explicit problem_check events.

        Args:
            line: text line from a tracking event log.

        Returns:
            (problem_id, username), (timestamp, problem_check_info)

            where timestamp is in ISO format, with resolution to the millisecond

            and problem_check_info is a JSON-serialized dict
            containing the contents of the problem_check event's
            'event' field, augmented with entries for 'timestamp',
            'username', and 'context' from the event.

            or None if there is no valid problem_check event on the line.

        Example:
                (i4x://edX/DemoX/Demo_Course/problem/PS1_P1, dummy_username), (2013-09-10T00:01:05.123456, blah)

        """
        parsed_tuple_or_none = get_problem_check_event(line)
        if parsed_tuple_or_none is not None:
            yield parsed_tuple_or_none

    def reducer(self, _key, values):
        """
        Calculate a list of answers from the final response of a user to a problem in a course.

        Args:
            key:  (problem_id, username)
            values:  iterator of (timestamp, problem_check_info)

        Yields:
            list of answer data tuples, where a tuple consists of:

                (course_id, answer_id), (timestamp, answer_data)

            where answer_data is a json-encoded dict, containing:

              'problem_id': the id of the problem (i4x)
              'problem_display_name': the display name for the problem
              'answer': if an event with 'submission' information,
                 this is the text of the answer.  For events with no
                 'submission' information, this is not defined.
              'answer_value_id': if an event with 'submission'
                 information, this is the moniker for the answer, and
                 is not defined if there is no moniker.  For events
                 with no 'submission' information, this holds either
                 the moniker (if used) or the answer (if no moniker is
                 used).
              'question': the display text for the problem part being answered, if available.
              'correct': boolean if the answer is correct.
              'variant': seed value

        """
        # Sort input values (by timestamp) to easily detect the most
        # recent answer to a problem by a particular user.  Note that
        # this assumes the timestamp values (strings) are in ISO
        # representation, so that the tuples will be ordered in
        # ascending time value.
        values = sorted(values)
        if not values:
            return

        # Get the last entry.
        _timestamp, most_recent_event = values[-1]

        for answer in self._generate_answers(most_recent_event):
            yield answer

    def _generate_answers(self, event_string):
        """
        Generates a list of answers given a problem_check event.

        Args:
            event_string:  a json-encoded string version of an event's data.

        Returns:
            list of answer data tuples.

        See docstring for reducer() for more details.
        """
        event = json.loads(event_string)

        # Get context information:
        course_id = event.get('context').get('course_id')
        timestamp = event.get('timestamp')
        problem_id = event.get('problem_id')
        problem_display_name = event.get('context').get('module', {}).get('display_name', None)
        result = []

        def append_submission(answer_id, submission):
            """Convert submission to result to be returned."""
            # First augment submission with problem-level information
            # not found in the submission:
            submission['problem_id'] = problem_id
            submission['problem_display_name'] = problem_display_name

            # Add the timestamp so that all responses can be sorted in order.
            # We want to use the "latest" values for some fields.
            output_key = (course_id, answer_id)
            output_value = (timestamp, json.dumps(submission))
            result.append((output_key, output_value))

        answers = event.get('answers')
        if 'submission' in event:
            submissions = event.get('submission')
            for answer_id in submissions:
                if not self.is_hidden_answer(answer_id):
                    submission = submissions.get(answer_id)
                    # But submission doesn't contain moniker value for answer.
                    # So we check the raw answers, and see if its value is
                    # different.  If so, we assume it's a moniker.
                    answer_value = answers[answer_id]
                    if answer_value != submission.get('answer'):
                        submission['answer_value_id'] = answer_value

                    append_submission(answer_id, submission)

        else:
            # Otherwise, it's an older event with no 'submission'
            # information, so parse it as well as possible.
            answers = event.get('answers')
            correct_map = event.get('correct_map')
            for answer_id in answers:
                if not self.is_hidden_answer(answer_id):
                    answer_value = answers[answer_id]

                    # Argh. It seems that sometimes we're encountering
                    # bogus answer_id values.  In particular, one that
                    # is including the possible choice values, instead
                    # of any actual values selected by the student.
                    # For now, let's just dump an error and skip it,
                    # so that it becomes the equivalent of a hidden
                    # answer.

                    # TODO: Eventually treat it explicitly as a hidden
                    # answer.
                    if answer_id not in correct_map:
                        log.error("Unexpected answer_id %s not in correct_map: %s", answer_id, event)
                        continue
                    correctness = correct_map[answer_id].get('correctness') == 'correct'

                    variant = event.get('state', {}).get('seed')

                    # We do not know the values for 'input_type',
                    # 'response_type', or 'question'.  We also don't know if
                    # answer_value should be identified as 'answer_value_id' or
                    # 'answer', so we choose to use 'answer_value_id' here and
                    # never define 'answer'.  This allows disambiguation from
                    # events with a submission field, which will always have
                    # an 'answer' and only sometimes have an 'answer_value_id'.
                    submission = {
                        'answer_value_id': answer_value,
                        'correct': correctness,
                        'variant': variant,
                    }
                    append_submission(answer_id, submission)

        return result

    def is_hidden_answer(self, answer_id):
        """Check Id to identify hidden kinds of values."""
        # some problems have additional answers that have '_dynamath' appended
        # to the regular answer_id.  In this case, the contents seem to contain
        # something like:
        #
        # <math xmlns="http://www.w3.org/1998/Math/MathML">
        #   <mstyle displaystyle="true">
        #     <mo></mo>
        #   </mstyle>
        # </math>
        if answer_id.endswith('_dynamath'):
            return True

        # Others seem to end with _comment, and I don't know yet what these
        # look like.
        if answer_id.endswith('_comment'):
            return True

        return False


class AnswerDistributionPerCourseMixin(object):
    """Calculates answer distribution on a problem in a course, given per-user answers by date."""

    def mapper(self, line):
        """
        Args:  tab-delimited values in a single text line

        Yields:  (course_id, answer_id), (timestamp, answer_data)

        Example:
            (edX/DemoX/Demo_Course, i4x-edX-DemoX-problem-c554538a57664fac80783b99d9d6da7c_2_1),
                (2013-09-10T01:10:25.012345, TBD)
        """
        course_id, answer_id, date, answer_data = line.split('\t')
        yield (course_id, answer_id), (date, answer_data)

    def reducer(self, key, values):
        """
        Calculate a JSON dict for each unique answer to a problem in a course.

        Args:
            key:  (course_id, answer_id)
            values:  iterator of (timestamp, answer_data)

        Yields:
            list of answer data tuples, where a tuple consists of:

                course_id, answer_json

            where answer_json is a JSON string corresponding to a
            particular response value to a particular "answer" within
            a problem.  The JSON includes metadata about the particular
            answer, the value of the answer, and the count of how many
            users for whom it was an answer.

        """
        course_id, answer_id = key

        values = sorted(values)
        if not values:
            return

        # Get the last entry.  We will use its values to provide
        # metadata about the particular answer.
        _timestamp, most_recent_answer_string = values[-1]
        most_recent_answer = json.loads(most_recent_answer_string)

        self.add_metadata_to_answer(answer_id, most_recent_answer)

        # Determine if any answers should be included based on
        # information in the most recent answer.
        if not self.should_include_answer(most_recent_answer):
            return

        # Now construct answer distribution for this input.
        problem_id = most_recent_answer.get('problem_id')
        problem_display_name = most_recent_answer.get('problem_display_name')
        most_recent_question = most_recent_answer.get('question', '')
        answer_uses_value_id = ('answer_value_id' in most_recent_answer)
        answer_uses_variant = (most_recent_answer.get('variant', '') != '')
        answer_dist = {}
        for _timestamp, value_string in reversed(values):
            answer = json.loads(value_string)
            self.add_metadata_to_answer(answer_id, answer)
            answer_grouping_key = self.get_answer_grouping_key(answer)

            # TODO: add check here to see if the number of distinct
            # variants for the problem is high enough to trigger
            # abandoning the output of the distribution.

            # If this is the first entry we find that has this value,
            # then save out the relevant metadata about this value.
            # We only want this from the most recent answer that has
            # this value.
            if answer_grouping_key not in answer_dist:
                if answer_uses_value_id:
                    # The most recent overall answer indicates that
                    # the code should be returned as such.  If this
                    # particular answer did not have 'submission'
                    # information, it may not have an answer_value, so
                    # we flag it. The problem type may have changed as
                    # well, so previous answers to this problem may not
                    # actually have an answer_value_id even though the
                    # most recent one does.
                    value_id = answer.get('answer_value_id', '')
                    answer_value = answer.get('answer', UNKNOWN_ANSWER_VALUE)
                else:
                    # There should be no value_id returned.  If the
                    # current answer did not have 'submission'
                    # information, then move the value from the
                    # 'answer_value_id' to the 'answer' field.
                    value_id = ""
                    answer_value = answer.get('answer', answer.get('answer_value_id'))

                # These values may be lists, so convert to output format.
                # And if we have a value_id, the corresponding answer_value
                # may contain HTML markup, that should be stripped.
                # But don't strip markup otherwise, as it may be part of
                # the answer.
                value_id = self.stringify(value_id)
                answer_value_contains_html = (value_id is not None and value_id != '')
                answer_value = self.stringify(answer_value, contains_html=answer_value_contains_html)

                # If there is a variant, then the question might not be
                # the same for all variants presented to students.  So
                # we take the value (if any) provided in this variant.
                # If there is no variant, then the question should be
                # the same, and we want to go with the most recently
                # defined value.
                if answer_uses_variant:
                    question = answer.get('question', '')
                    variant = answer.get('variant') or ''
                else:
                    question = most_recent_question
                    variant = ''

                # Key values here should match those used in get_column_order().
                answer_dist[answer_grouping_key] = {
                    'ModuleID': problem_id,
                    'PartID': answer_id,
                    'ValueID': value_id or '',
                    'AnswerValue': answer_value or '',
                    'Variant': variant,
                    'Problem Display Name': problem_display_name or '',
                    'Question': question,
                    'Correct Answer': '1' if answer.get('correct') else '0',
                    'Count': 0,
                }

            # For most cases, just increment a counter:
            answer_dist[answer_grouping_key]['Count'] += 1

        # Finally dispatch the answers, providing the course_id as a
        # key so that the answers belonging to a course will be
        # gathered downstream into a report.
        for answer_entry in answer_dist.values():
            # Transform the entry into a form suitable for output.
            yield course_id, json.dumps(answer_entry)

    @classmethod
    def get_column_order(cls):
        """Return column order to use for Answer Distribution report."""
        # Key values here should match those used in the answer dict being output.
        return [
            'ModuleID',
            'PartID',
            'Correct Answer',
            'Count',
            'ValueID',
            'AnswerValue',
            'Variant',
            'Problem Display Name',
            'Question',
        ]

    def load_answer_metadata(self, answer_metadata_file):
        """
        Load metadata for answers that may lack it in problem_check events.

        Information is read from a JSON file, with dict keyed by
        answer_id, where "answer_id" is the i4x identifier for
        particular answer.

        Expected fields in dict are:

            "problem_display_name": contains display name of containing Problem.
            "input_type": xml element name for the input type.
            "response_type": xml element name for the response type.
            "question": contains question displayed to user.
            "answer_value_id_map": dict with key equal to 'answer_value_id' values,
                and displayed text as its value.

        Stores data internally as a dict, keyed on answer_id.

        This information was added to problem_check events, but this
        provides a mechanism for providing the information for those
        problem_check events that occurred before this addition was
        made.

        """
        self.answer_metadata_dict = json.load(answer_metadata_file)  # pylint: disable=attribute-defined-outside-init

    def add_metadata_to_answer(self, answer_id, answer):
        """
        Add externally-provided metadata for answers that lack it.

        See docstring for load_answer_metadata() for list of fields.

        Adds these fields to the answer if the answer lacks a
        non-empty value.  Uses the answer_value_id_map to provide a
        corresponding 'answer' when only an 'answer_value_id' is
        available.  These are done for answers that are derived from
        problem_check events that lack these fields, because they
        occurred before the information was added to events.

        """
        # The 'answer_metadata_dict' should only exist if load_answer_metadata() is called.
        answer_metadata = getattr(self, 'answer_metadata_dict', {}).get(answer_id)
        if answer_metadata is not None:
            for key, value in answer_metadata.iteritems():
                # Should only add values that are not already present
                # (and non-null).  Also skips over values that are not
                # strings (such as the answer_value_id_map), as this is
                # handled separately below.
                if not answer.get(key) and isinstance(value, basestring):
                    answer[key] = value

            if 'answer' not in answer:
                response_type = answer.get('response_type')
                if response_type in ['choiceresponse', 'multiplechoiceresponse']:
                    # We leave what we have in 'answer_value_id', and look
                    # up the 'answer' to use from the
                    # answer_metadata_dict, based on the value(s) in
                    # 'answer_value_id'.
                    if 'answer_value_id_map' in answer_metadata:
                        answer_value_id = answer['answer_value_id']
                        answer_value_id_map = answer_metadata['answer_value_id_map']
                        get_answer_value = lambda code: answer_value_id_map.get(code, UNMAPPED_ANSWER_VALUE)
                        if isinstance(answer_value_id, basestring):
                            answer['answer'] = get_answer_value(answer_value_id)
                        elif isinstance(answer_value_id, list):
                            answer['answer'] = [get_answer_value(code) for code in answer_value_id]
                else:
                    # The 'answer_value_id' is really the 'answer', so move it.
                    answer['answer'] = answer['answer_value_id']
                    del answer['answer_value_id']

    def should_include_answer(self, answer):
        """Determine if a problem "part" should be included in the distribution."""
        response_type = answer.get('response_type')

        # For problems which only have old responses, we don't
        # have information about whether to include their answers.
        if response_type is None:
            return False

        # At some point, we could make this more parameterized, but
        # support for other types would likely require special
        # handling here anyway.
        valid_types = set([
            'choiceresponse',
            'optionresponse',
            'multiplechoiceresponse',
            'numericalresponse',
            'stringresponse',
            'formularesponse',
        ])
        if response_type in valid_types:
            return True

        return False

    def get_answer_grouping_key(self, answer):
        """Return value to use for uniquely identify an answer value in the distribution."""
        # For variants, we want to treat missing variants with the
        # same value as used for events that lack 'submission'
        # information, so that they will be grouped together.  That
        # value is a seed value of '1'.  We want to map both missing
        # values and zero-length values to this default value.
        variant = answer.get('variant', '')
        if variant == '':
            variant = '1'
        # Events that lack 'submission' information will have a value
        # for 'answer_value_id' and none for 'answer'.  Events with
        # 'submission' information will have the reverse situation
        # most of the time, but both values filled in for multiple
        # choice.  In the latter case, we need to use the
        # answer_value_id for comparison.
        if 'answer_value_id' in answer:
            answer_value = answer.get('answer_value_id')
        else:
            answer_value = answer.get('answer')

        # answer_value may be a list of multiple values, so we need to
        # convert it to a string that can be used as an index (i.e. to
        # increment a previous occurrence).
        return u'{value}_{variant}'.format(value=self.stringify(answer_value), variant=variant)

    @staticmethod
    def stringify(answer_value, contains_html=False):
        """
        Convert answer value to a canonical string representation.

        If answer_value is a list, then returns list values
        surrounded by square brackets and delimited by pipes
        (e.g. "[choice_1|choice_3|choice_4]").

        If answer_value is a string, just returns as-is.

        If contains_html is True, the answer_string is parsed as XML,
        and the text value of the answer_value is returned.

        """
        # If it's a list, convert to a string.  Note that it's not
        # enough to call str() or unicode(), as this will appear as
        # "[u'choice_5']".
        def normalize(value):
            """Pull out HTML tags if requested."""
            return get_text_from_html(value) if contains_html else value.strip()

        if isinstance(answer_value, basestring):
            return normalize(answer_value)
        elif isinstance(answer_value, list):
            list_val = u'|'.join(normalize(value) for value in answer_value)
            return u'[{list_val}]'.format(list_val=list_val)
        else:
            # unexpected type:
            log.error("Unexpected type for an answer_value: %s", answer_value)
            return unicode(answer_value)


def get_text_from_html(markup):
    """
    Convert html markup to plain text.

    Includes stripping excess whitespace, and assuring whitespace
    exists between elements (e.g. table elements).
    """
    try:
        root = html5lib.parse(markup)
        text_list = []
        for val in get_text_from_element(root):
            text_list.extend(val.split())
        text = u' '.join(text_list)
    except Exception as exception:  # pylint: disable=broad-except
        # TODO: find out what exceptions might actually occur here, if any.
        # This may be unnecessarily paranoid, given html5lib's fallback behavior.
        log.error("Unparseable answer value markup: '%s' return exception %s", markup, exception)
        text = markup.strip()

    return text


def get_text_from_element(node):
    """Traverse ElementTree node recursively to return text values."""
    tag = node.tag
    if not isinstance(tag, basestring) and tag is not None:
        return
    if node.text:
        yield node.text
    for child in node:
        for text in get_text_from_element(child):
            yield text
        if child.tail:
            yield child.tail


##################################
# Task requires/output definitions
##################################

class BaseAnswerDistributionTask(MapReduceJobTask):
    """
    Base class for answer distribution calculations.

    Parameters:
        name: a unique identifier to distinguish one run from another.  It is used in
            the construction of output filenames, so each run will have distinct outputs.
        src:  a URL to the root location of input tracking log files.
        dest:  a URL to the root location to write output file(s).
        include:  a list of patterns to be used to match input files, relative to `src` URL.
            The default value is ['*'].
        manifest: a URL to a file location that can store the complete set of input files.
    """
    name = luigi.Parameter()
    src = luigi.Parameter()
    dest = luigi.Parameter()
    include = luigi.Parameter(is_list=True, default=('*',))
    # A manifest file is required by hadoop if there are too many input paths. It hits an operating system limit on the
    # number of arguments passed to the mapper process on the task nodes.
    manifest = luigi.Parameter(default=None)

    def extra_modules(self):
        import six
        return [html5lib, six]


class LastProblemCheckEvent(LastProblemCheckEventMixin, BaseAnswerDistributionTask):
    """Identifies last problem_check event for a user on a problem in a course, given raw event log input."""

    def requires(self):
        return PathSetTask(self.src, self.include, self.manifest)

    def output(self):
        output_name = u'last_problem_check_events_{name}/'.format(name=self.name)
        return get_target_from_url(url_path_join(self.dest, output_name))


class AnswerDistributionPerCourse(AnswerDistributionPerCourseMixin, BaseAnswerDistributionTask):
    """
    Calculates answer distribution on a problem in a course, given per-user answers by date.


    Additional Parameters:
        answer_metadata:  optional file to provide information about particular answers.
            Includes problem_display_name, input_type, response_type, and question.
        base_input_format:  The input format to use on the first map reduce job in the chain. This job takes in the most
            input and may need a custom input format.
    """

    answer_metadata = luigi.Parameter(default=None)
    base_input_format = luigi.Parameter(default=None)

    def requires(self):
        results = {
            'events': LastProblemCheckEvent(
                mapreduce_engine=self.mapreduce_engine,
                input_format=self.base_input_format,
                lib_jar=self.lib_jar,
                n_reduce_tasks=self.n_reduce_tasks,
                name=self.name,
                src=self.src,
                dest=self.dest,
                include=self.include,
                manifest=self.manifest,
            ),
        }

        if self.answer_metadata:
            results.update({'answer_metadata': ExternalURL(self.answer_metadata)})

        return results

    def requires_hadoop(self):
        # Only pass the input files on to hadoop, not any metadata file.
        return self.requires()['events']

    def output(self):
        output_name = u'answer_distribution_per_course_{name}/'.format(name=self.name)
        return get_target_from_url(url_path_join(self.dest, output_name))

    def run(self):
        # Define answer_metadata on the object if specified.
        if 'answer_metadata' in self.input():
            with self.input()['answer_metadata'].open('r') as answer_metadata_file:
                self.load_answer_metadata(answer_metadata_file)

        super(AnswerDistributionPerCourse, self).run()


class AnswerDistributionOneFilePerCourseTask(MultiOutputMapReduceJobTask):
    """
    Groups answer distributions by course, producing a different file for each.

    Most parameters are passed through to :py:class:`AnswerDistributionPerCourse`.
    Additional parameters are defined by :py:class:`MultiOutputMapReduceJobTask`.:

        output_root: location where the one-file-per-course outputs
            are written.  This is distinct from `dest`, which is where
            intermediate output is written.
        delete_output_root: if True, recursively deletes the output_root at task creation.
    """

    src = luigi.Parameter()
    dest = luigi.Parameter()
    include = luigi.Parameter(is_list=True, default=('*',))
    name = luigi.Parameter(default='periodic')
    answer_metadata = luigi.Parameter(default=None)
    manifest = luigi.Parameter(default=None)
    base_input_format = luigi.Parameter(default=None)

    def output(self):
        # Because this task writes to a shared directory, we don't
        # want to include a marker for job success.  Use a special
        # target that always triggers new runs and never writes out.
        return IgnoredTarget()

    def requires(self):
        return AnswerDistributionPerCourse(
            mapreduce_engine=self.mapreduce_engine,
            lib_jar=self.lib_jar,
            base_input_format=self.base_input_format,
            n_reduce_tasks=self.n_reduce_tasks,
            src=self.src,
            dest=self.dest,
            include=self.include,
            name=self.name,
            answer_metadata=self.answer_metadata,
            manifest=self.manifest,
        )

    def mapper(self, line):
        """
        Groups inputs by course_id, writes all records with the same course_id to the same output file.

        Each input line is expected to consist of two tab separated columns. The first column is expected to be the
        course_id and is used to group the entries. The course_id is stripped from the output and the remaining column
        is written to the appropriate output file in the same format it was read in (i.e. as an encoded JSON string).
        """
        # Ensure that the first column is interpreted as the grouping key by the hadoop streaming API.  Note that since
        # Configuration values can change this behavior, the remaining tab separated columns are encoded in a python
        # structure before returning to hadoop.  They are decoded in the reducer.
        course_id, content = line.split('\t')
        yield course_id, content

    def output_path_for_key(self, course_id):
        """
        Match the course folder hierarchy that is expected by the instructor dashboard.

        The instructor dashboard expects the file to be stored in a folder named sha1(course_id).  All files in that
        directory will be displayed on the instructor dashboard for that course.
        """
        hashed_course_id = hashlib.sha1(course_id).hexdigest()
        filename_safe_course_id = course_id.replace('/', '_')
        filename = u'{course_id}_answer_distribution.csv'.format(course_id=filename_safe_course_id)
        return url_path_join(self.output_root, hashed_course_id, filename)

    def multi_output_reducer(self, _course_id, values, output_file):
        """
        Each entry should be written to the output file in csv format.

        This output is visible to instructors, so use an excel friendly format (csv).
        """
        field_names = AnswerDistributionPerCourse.get_column_order()
        writer = csv.DictWriter(output_file, field_names)
        writer.writerow(dict(
            (k, k) for k in field_names
        ))

        # Collect in memory the list of dicts to be output.  Then sort
        # the list of dicts by their field names before encoding.
        row_data = [json.loads(content) for content in values]
        row_data = sorted(row_data, key=itemgetter(*field_names))

        for row_dict in row_data:
            encoded_dict = dict()
            for key, value in row_dict.iteritems():
                encoded_dict[key] = unicode(value).encode('utf8')
            writer.writerow(encoded_dict)

    def extra_modules(self):
        import six
        return [html5lib, six]


################################
# Helper methods
################################

def get_problem_check_event(line):
    """
    Generates output values for explicit problem_check events.

    Args:

        line: text line from a tracking event log.

    Returns:

        (problem_id, username), (timestamp, problem_check_info)

        where timestamp is in ISO format, with resolution to the millisecond
        and problem_check_info is a JSON-serialized dict containing
        the contents of the problem_check event's 'event' field,
        augmented with entries for 'timestamp', 'username', and
        'context' from the event.

        or None if there is no valid problem_check event on the line.

    Example:
            (i4x://edX/DemoX/Demo_Course/problem/PS1_P1, dummy_username), (2013-09-10T00:01:05.123456, blah)

    """
    # Parse the line into a dict.
    event = eventlog.parse_json_server_event(line, 'problem_check')
    if event is None:
        return None

    # Get the "problem data".  This is the event data, the context, and anything else that would
    # be useful further downstream.  (We could just pass the entire event dict?)

    # Get the user from the username, not from the user_id in the
    # context.  While we are currently requiring context (as described
    # above), we might not in future.  Older events will not have
    # context information, so we can't rely on user_id from there.
    # And we don't expect problem_check events to occur without a
    # username, and don't expect them to occur with the wrong user
    # (i.e. one user acting on behalf of another, as in an instructor
    # acting on behalf of a student).
    augmented_data_fields = ['context', 'username', 'timestamp']
    problem_data = eventlog.get_augmented_event_data(event, augmented_data_fields)
    if problem_data is None:
        return None

    # Get the course_id from context.  We won't work with older events
    # that do not have context information, since they do not directly
    # provide course_id information.  (The problem_id/answer_id values
    # contain the org and course name, but not the run.)  Course_id
    # information could be found from other events, but it would
    # require expanding the events being selected.
    course_id = problem_data.get('context').get('course_id')
    if course_id is None:
        log.error("encountered explicit problem_check event with missing course_id: %s", event)
        return None

    if not eventlog.is_valid_course_id(course_id):
        log.error("encountered explicit problem_check event with bogus course_id: %s", event)
        return None

    # Get the problem_id from the event data.
    problem_id = problem_data.get('problem_id')
    if problem_id is None:
        log.error("encountered explicit problem_check event with bogus problem_id: %s", event)
        return None

    problem_data_json = json.dumps(problem_data)
    key = (course_id, problem_id, problem_data.get('username'))
    value = (problem_data.get('timestamp'), problem_data_json)

    return key, value
