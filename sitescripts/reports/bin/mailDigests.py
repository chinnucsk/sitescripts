# coding: utf-8

# This Source Code is subject to the terms of the Mozilla Public License
# version 2.0 (the "License"). You can obtain a copy of the License at
# http://mozilla.org/MPL/2.0/.

import MySQLdb, os, sys, re, marshal
from datetime import date
from time import time
from email.utils import parseaddr
from sitescripts.utils import get_config, setupStderr
from sitescripts.templateFilters import formatmime
from sitescripts.reports.utils import mailDigest, calculateReportSecret, getDigestId, getDigestSecret, get_db, executeQuery
import sitescripts.subscriptions.subscriptionParser as subscriptionParser

def loadSubscriptions():
  global interval, weekDay

  subscriptions = subscriptionParser.readSubscriptions()

  results = {}
  resultList = []
  for subscription in subscriptions.values():
    if subscription.digest == 'daily' and interval == 'week':
      continue
    if subscription.digest == 'weekly' and interval == 'day':
      continue
    if interval == 'week' and subscription.digestDay != weekDay:
      continue

    for [title, url, complete] in subscription.variants:
      results[url] = subscription
    resultList.append(subscription)
  return (results, resultList)

def scanReports():
  global fakeSubscription, interval, subscriptions, startTime

  result = []
  cursor = get_db().cursor(MySQLdb.cursors.DictCursor)
  executeQuery(cursor,
              '''SELECT guid, dump FROM #PFX#reports WHERE ctime >= FROM_UNIXTIME(%s)''',
              (startTime))

  for report in cursor:
    reportData = marshal.loads(report['dump'])

    matchSubscriptions = {}
    for filter in reportData.get('filters', []):
      for url in filter.get('subscriptions', []):
        if url in subscriptions:
          matchSubscriptions[url] = subscriptions[url]

    recipients = []
    reportType = reportData.get('type', 'unknown')
    if reportType == 'false positive' or reportType == 'false negative':
      for subscription in reportData.get('subscriptions', []):
        subscriptionID = subscription.get('id', 'unknown')
        # Send false negatives to all subscription authors, false positives
        # only to subscriptions with matching filters
        if subscriptionID in subscriptions and (reportType == 'false negative' or subscriptionID in matchSubscriptions):
          recipients.append(subscriptions[subscriptionID])
    elif interval != 'week':
      # Send type "other" to fake subscription - daily reports
      recipients.append(fakeSubscription)

    if len(recipients) == 0:
      continue

    report = {
      'url': get_config().get('reports', 'urlRoot') + report['guid'] + '#secret=' + calculateReportSecret(report['guid']),
      'weight': calculateReportWeight(reportData),
      'site': reportData.get('siteName', 'unknown'),
      'subscriptions': recipients,
      'comment': re.sub(r'[\x00-\x20]', r' ', reportData.get('comment', '')),
      'type': reportData.get('type', 'unknown'),
      'numSubscriptions': len(reportData.get('subscriptions', [])),
      'matchSubscriptions': matchSubscriptions.values(),
      'email': reportData.get('email', None),
      'screenshot': reportData.get('screenshot', None) != None,
      'screenshotEdited': reportData.get('screenshotEdited', False),
      'knownIssues': len(reportData.get('knownIssues', [])),
    }
    result.append(report)
  return result

def sendNotifications(reports):
  global subscriptionList

  for subscription in subscriptionList:
    selectedReports = filter(lambda report: subscription in report['subscriptions'], reports)
    if len(selectedReports) == 0:
      continue

    groups = {}
    for report in selectedReports:
      if report['site'] in groups:
        groups[report['site']]['reports'].append(report)
        groups[report['site']]['weight'] += report['weight']
      else:
        groups[report['site']] = {'name': report['site'], 'reports': [report], 'weight': report['weight'], 'dumpAll': False}

    miscGroup = {'name': 'Misc', 'reports': [], 'weight': None, 'dumpAll': True}
    for (site, group) in groups.items():
      if len(group['reports']) == 1:
        miscGroup['reports'].append(group['reports'][0])
        del groups[site]

    if len(miscGroup['reports']) > 0:
      groups[miscGroup['name']] = miscGroup

    groups = groups.values()
    groups.sort(lambda a,b: -cmp(a['weight'], b['weight']))
    for group in groups:
      group['reports'].sort(lambda a,b: -cmp(a['weight'], b['weight']))

    sendMail(subscription, groups)

def sendMail(subscription, groups):
  if hasattr(subscription, 'email'):
    email = subscription.email
  else:
    email = subscription['email']

  name, address = parseaddr(email)
  email = formatmime(name) + ' <' + formatmime(address) + '>'
  
  id = getDigestId(address)
  digestLink = get_config().get('reports', 'urlRoot') + 'digest?id=%s&secret=%s' % (id, getDigestSecret(id, date.today().isocalendar()))

  mailDigest({'email': email, 'digestLink': digestLink, 'subscription': subscription, 'groups': groups})

def calculateReportWeight(reportData):
  global currentTime, startTime

  weight = 1.0
  if reportData.get('type', 'unknown') == 'false positive' or reportData.get('type', 'unknown') == 'false negative':
    weight /= len(reportData.get('subscriptions', []))
  if 'screenshot' in reportData and reportData.get('screenshotEdited', False):
    weight += 0.7
  elif 'screenshot' in reportData:
    weight += 0.3
  if len(reportData.get('knownIssues', [])) > 0:
    weight -= 0.3
  if re.search(r'\btest\b', reportData.get('comment', ''), re.IGNORECASE):
    weight -= 0.5
  elif re.search(r'\S', reportData.get('comment', '')):
    weight += 0.5
  if 'email' in reportData:
    weight += 0.3

  weight += (reportData.get('time', 0) - startTime) / (currentTime - startTime) * 0.2
  return weight

if __name__ == '__main__':
  setupStderr()

  if len(sys.argv) < 2:
    raise Exception('No interval specified')

  interval = sys.argv[1]
  if not (interval in ['all', 'week', 'day']):
    raise Exception('Invalid interval')

  if interval == 'week' and len(sys.argv) < 3:
    raise Exception('No weekday specified')
  weekDay = int(sys.argv[2]) if interval == 'week' else -1

  currentTime = time()
  startTime = 0
  if interval == 'week':
    startTime = currentTime - 7*24*60*60
  elif interval == 'day':
    startTime = currentTime - 24*60*60

  fakeSubscription = {'url': 'https://fake.adblockplus.org', 'name': get_config().get('reports', 'defaultSubscriptionName'), 'email': get_config().get('reports', 'defaultSubscriptionRecipient')}
  (subscriptions, subscriptionList) = loadSubscriptions()
  subscriptionList.append(fakeSubscription)
  reports = scanReports()
  sendNotifications(reports)
