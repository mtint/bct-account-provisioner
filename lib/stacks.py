from hashlib import sha1
import logging

import boto3
from botocore.exceptions import ClientError, WaiterError

logger = logging.getLogger(__name__)


class Stack:
    """CRUD for CloudFormation Stack.

    Args:
        stack_name:     name of CFN stack
        stack_arn:      ARN of CFN stack
        cfn_client:     boto3 cloudformation client that should be used,
                        default client will be used if one is not provided
        """

    def __init__(self, stack_name, stack_arn=None, cfn_client=None):
        self._arn = stack_arn
        self._cfn = (cfn_client if cfn_client
                     else boto3.client('cloudformation'))
        self._hexdigest = None
        self._name = stack_name
        self._parameters = None

    def __str__(self):
        return str(self.to_dict())

    def apply_template(self, template, parameters=None):
        """applies a cfn template to stack.

        This may create the stack from scratch or update an existing stack.
        Will clean up stacks that are in "ROLLBACK_COMPLETE" state before
        trying to create a stack.

        Arg:
            template:       A string obj of the CFN template.
            parameters:     A dict of parameters to be used when creating the
                            CFN stack.

        """
        # Check if stack already exists, if rolled back, then _delete stack
        if self.status == 'ROLLBACK_COMPLETE':
            logger.warning(
                "CFN stack {} in a ROLLBACK_COMPLETE state.".format(self.name)
            )
            self._delete()

        if parameters:
            cfn_params = [
                {'ParameterKey': key,
                 'ParameterValue': parameters[key]}
                for key in parameters]
        else:
            cfn_params = []
        logger.debug("CFN Params to be used: {}".format(cfn_params))

        # First see if the supplied template is the same as what is
        # currently used for the stack. This is done by comparing the
        # hexdigest of the template with the stack. If the stack's hexdigest
        # is none, then that means the stack does not exist and should be
        # created.
        stack_updated = False
        if not template.endswith('\n'):
            template = template + '\n'
        hexdigest = sha1(template.encode()).hexdigest()
        if self.hexdigest is None:
            self._create(template, cfn_params)
            stack_updated = True
        elif hexdigest != self.hexdigest:
            self._update(template, cfn_params)
            stack_updated = True
            # remove cached hexdigest
            self._hexdigest = None

        # If the stack hasn't been updated then check to see if the supplied
        #  params match the stacks current params. If they don't match then
        # the params have been updated and the stack needs to be updated.
        if not stack_updated:
            stack_params = {
                stack_param['ParameterKey']: stack_param['ParameterValue']
                for stack_param in self.parameters
            }
            for param in cfn_params:
                key = param['ParameterKey']
                value = param['ParameterValue']
                stack_value = stack_params.get(key, '')
                if value != stack_value:
                    logger.debug(
                        "Template param {}:{} differs from "
                        "stack param {}:{}".format(key, value,
                                                   key, stack_value)
                    )
                    self._update(template, cfn_params)
                    stack_updated = True

        if not stack_updated:
            logger.info("CFN stack {} already up-to-date.".format(self.name))

    @property
    def arn(self):
        if self.status:
            if not self._arn:
                try:
                    response = self._cfn.describe_stacks(StackName=self.name)
                except ClientError as e:
                    if "does not exist" in e.response['Error']['Message']:
                        return None
                    else:
                        raise
                self._arn = response['Stacks'][0]['StackId']
        else:
            self._arn = None
        return self._arn

    @arn.setter
    def arn(self, arn):
        self._arn = arn

    def __cfn_wait(self, condition):
        create_waiter = self._cfn.get_waiter(condition)
        waiter_delay = 10
        waiter_max_attempts = 18
        logger.info("Waiting up to {} seconds for {}.".format(
            str(waiter_delay * waiter_max_attempts),
            condition
        ))
        try:
            create_waiter.wait(StackName=self.arn,
                               WaiterConfig={
                                   'Delay': waiter_delay,
                                   'MaxAttempts': waiter_max_attempts
                               }
                               )
        except WaiterError:
            events_response = self._cfn.describe_stack_events(
                StackName=self.name)
            for event in events_response['StackEvents']:
                if event['ResourceStatus'] == 'CREATE_FAILED':
                    logger.error(event['ResourceStatusReason'])
            raise RuntimeError(
                "Stack failed to _create with status {}".format(
                    self.status))

    def _create(self, template, param_list=None):
            logger.info(
                "Creating CFN stack {} with Params {}".format(
                    self.name,
                    param_list
                )
            )
            response = self._cfn.create_stack(
                StackName=self.name,
                TemplateBody=template,
                Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM'],
                Parameters=param_list
            )

            arn = response['StackId']
            logger.info("StackId {}".format(arn))
            self.__cfn_wait('stack_create_complete')
            logger.info("Stack {} created".format(self.name))

    def _delete(self):
        arn = self.arn
        logger.info("Deleting stack with ARN {}".format(arn))
        self._cfn.delete_stack(StackName=arn)
        self.__cfn_wait('stack_delete_complete')
        logger.info("Stack {} deleted".format(self.name))

    @property
    def hexdigest(self):
        if self.status:
            if not self._hexdigest:
                cfn_response = self._cfn.get_template(StackName=self.arn)
                stack_template = cfn_response['TemplateBody']
                self._hexdigest = sha1(stack_template.encode()).hexdigest()
            return self._hexdigest
        else:
            return None

    @hexdigest.setter
    def hexdigest(self, digest):
        self._hexdigest = digest

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, name):
        self._name = name

    @property
    def parameters(self):
        if self.status:
            if not self._parameters:
                try:
                    response = self._cfn.describe_stacks(StackName=self.name)
                except ClientError as e:
                    if "does not exist" in e.response['Error']['Message']:
                        return None
                    else:
                        raise
                self._parameters = response['Stacks'][0]['Parameters']
        else:
            self._parameters = None
        return self._parameters

    @property
    def status(self):
        try:
            response = self._cfn.describe_stacks(StackName=self.name)
        except ClientError as e:
            if "does not exist" in e.response['Error']['Message']:
                return None
            else:
                raise
        return response['Stacks'][0]['StackStatus']

    def to_dict(self):
        return {
                'arn': self.arn,
                'hexdigest': self.hexdigest,
                'name': self.name,
                'status': self.status
        }

    def _update(self, template, param_list=None):
        logger.info(
            "Updating CFN stack {} with Params {}".format(
                self.name,
                param_list
            )
        )
        self._cfn.update_stack(
            StackName=self.name,
            TemplateBody=template,
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM'],
            Parameters=param_list
        )
        self.__cfn_wait('stack_update_complete')
        logger.info("Stack {} updated".format(self.name))


class Template:
    """Reads and validates a template from S3 or file.

    Args:
        template_path:   Path to Template, either in file:// or s3:// format

    """

    def __init__(self, template_path):
        if (template_path.startswith('file://')
            or template_path.startswith('s3://')):
                self._template_path = template_path
        else:
            raise ValueError(
                "template_path must start with 'file://' or 's3://'"
            )
        self._body = self.__read_template()
        self._hexdigest = sha1(self._body.encode()).hexdigest()

    def __str__(self):
        return self.body

    @property
    def body(self):
        return self._body

    @property
    def hexdigest(self):
        return self._hexdigest

    def __read_template(self):
        s3 = boto3.client('s3')
        cfn = boto3.client('cloudformation')
        if self._template_path.startswith('file://'):
            file_path = self._template_path.replace('file://', '')
            with open(file_path) as template_file:
                template_body = template_file.read()
        elif self._template_path.startswith('s3://'):
            s3_path = self._template_path.replace('s3://', '')
            s3_bucket, s3_key = s3_path.split('/', maxsplit=1)
            response = s3.get_object(Bucket=s3_bucket,
                                     Key=s3_key)
            # response is a binary obj, need to decode it into a string
            template_body = response['Body'].read().decode()
        else:
            raise ValueError(
                "Invalid template_path: {}".format(self._template_path)
            )
        # add new line to end of file if one doesn't exist
        # this is needed when comparing templates with each other
        if not template_body.endswith('\n'):
            template_body = template_body + '\n'
        return template_body
