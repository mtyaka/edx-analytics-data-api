FROM ubuntu:focal as app

# System requirements.
RUN apt update && \
  apt-get install -qy \ 
  curl \
  vim \
  git-core \
  language-pack-en \
  build-essential \
  python3.8-dev \
  python3-virtualenv \
  python3.8-distutils \
  libmysqlclient-dev \
  libssl-dev && \
  rm -rf /var/lib/apt/lists/*

# Use UTF-8.
RUN locale-gen en_US.UTF-8
ENV LANG en_US.UTF-8
ENV LANGUAGE en_US:en
ENV LC_ALL en_US.UTF-8

ARG COMMON_APP_DIR="/edx/app"
ARG ANALYTICS_API_SERVICE_NAME="analytics_api"
ENV ANALYTICS_API_HOME "${COMMON_APP_DIR}/${ANALYTICS_API_SERVICE_NAME}"
ARG ANALYTICS_API_APP_DIR="${COMMON_APP_DIR}/${ANALYTICS_API_SERVICE_NAME}"
ARG SUPERVISOR_APP_DIR="${COMMON_APP_DIR}/supervisor"
ARG ANALYTICS_API_VENV_DIR="${COMMON_APP_DIR}/${ANALYTICS_API_SERVICE_NAME}/venvs/${ANALYTICS_API_SERVICE_NAME}"
ARG SUPERVISOR_VENVS_DIR="${SUPERVISOR_APP_DIR}/venvs"
ARG SUPERVISOR_VENV_DIR="${SUPERVISOR_VENVS_DIR}/supervisor"
ARG ANALYTICS_API_CODE_DIR="${ANALYTICS_API_APP_DIR}/${ANALYTICS_API_SERVICE_NAME}"
ARG SUPERVISOR_AVAILABLE_DIR="${COMMON_APP_DIR}/supervisor/conf.available.d"
ARG SUPERVISOR_VENV_BIN="${SUPERVISOR_VENV_DIR}/bin"
ARG SUPEVISOR_CTL="${SUPERVISOR_VENV_BIN}/supervisorctl"
ARG SUPERVISOR_VERSION="4.2.1"
ARG SUPERVISOR_CFG_DIR="${SUPERVISOR_APP_DIR}/conf.d"


ENV HOME /root
ENV PATH "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
ENV PATH "${ANALYTICS_API_VENV_DIR}/bin:$PATH"
ENV COMMON_CFG_DIR "/edx/etc"
ENV ANALYTICS_API_CFG "/edx/etc/${ANALYTICS_API_SERVICE_NAME}.yml"


RUN addgroup analytics_api
RUN adduser --disabled-login --disabled-password analytics_api --ingroup analytics_api

# Make necessary directories and environment variables.
RUN mkdir -p /edx/var/analytics_api/staticfiles
RUN mkdir -p /edx/var/analytics_api/media
# Log dir
RUN mkdir /edx/var/log/

RUN virtualenv -p python3.8 --always-copy ${ANALYTICS_API_VENV_DIR}
RUN virtualenv -p python3.8 --always-copy ${SUPERVISOR_VENV_DIR}


#install supervisor and deps in its virtualenv
RUN . ${SUPERVISOR_VENV_BIN}/activate && \
  pip install supervisor==${SUPERVISOR_VERSION} backoff==1.4.3 boto==2.48.0 && \
  deactivate

COPY requirements/production.txt ${ANALYTICS_API_CODE_DIR}/requirements/production.txt

RUN pip install -r ${ANALYTICS_API_CODE_DIR}/requirements/production.txt

# Working directory will be root of repo.
WORKDIR ${ANALYTICS_API_CODE_DIR}

# Copy over rest of code.
# We do this AFTER requirements so that the requirements cache isn't busted
# every time any bit of code is changed.
COPY . .
COPY /configuration_files/analytics_api_gunicorn.py ${ANALYTICS_API_HOME}/analytics_api_gunicorn.py
# deleted this file completely and defined the env variables in dockerfile's respective target images.
# COPY configuration_files/analytics_api_env ${ANALYTICS_API_HOME}/analytics_api_env
COPY /configuration_files/analytics_api.yml ${ANALYTICS_API_CFG}
COPY /scripts/analytics_api.sh ${ANALYTICS_API_HOME}/analytics_api.sh
# # create supervisor job
COPY /configuration_files/supervisor.service /etc/systemd/system/supervisor.service
COPY /configuration_files/analytics_api.conf ${SUPERVISOR_CFG_DIR}/supervisor.conf
COPY /configuration_files/supervisorctl ${SUPERVISOR_VENV_BIN}/supervisorctl
# # Manage.py symlink
COPY /manage.py /edx/bin/manage.analytics_api

# Expose canonical Analytics port
EXPOSE 19001

FROM app as prod

ENV DJANGO_SETTINGS_MODULE "analyticsdataserver.settings.production"

RUN make static

ENTRYPOINT ["/edx/app/analytics_api/analytics_api.sh"]

FROM app as dev

ENV DJANGO_SETTINGS_MODULE "analyticsdataserver.settings.devstack"

RUN pip install -r ${ANALYTICS_API_CODE_DIR}/requirements/dev.txt

COPY /scripts/devstack.sh ${ANALYTICS_API_HOME}/devstack.sh

RUN chown analytics_api:analytics_api /edx/app/analytics_api/devstack.sh && chmod a+x /edx/app/analytics_api/devstack.sh

# Devstack related step for backwards compatibility
RUN touch /edx/app/${ANALYTICS_API_SERVICE_NAME}/${ANALYTICS_API_SERVICE_NAME}_env

ENTRYPOINT ["/edx/app/analytics_api/devstack.sh"]
CMD ["start"]
