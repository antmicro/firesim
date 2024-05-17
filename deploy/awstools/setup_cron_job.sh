#!/bin/bash

set -e

mkdir -p ~/.cron
cp scripts/cron_job.py ~/.cron/cron_job.py
sudo cp scripts/centos /var/spool/cron/centos
sudo cp scripts/cleanup /etc/rc.d/init.d/cleanup
sudo systemctl daemon-reload
sudo systemctl enable cleanup
sudo systemctl start cleanup
