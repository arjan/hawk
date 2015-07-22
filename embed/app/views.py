"""Module which defines views - actions for url passed requests"""

import sys
import os
import re
from urlparse import urlparse
import time
import gzip

from flask import request, render_template, abort, url_for, g
import simplejson as json
from flask import current_app as app
import bleach

from iiif_manifest_factory import ManifestFactory
from ingest import ingestQueue
from models import Item, Batch, Task
from exceptions import NoItemInDb, ErrorItemImport
from helper import prepareTileSources, getCloudSearch


# Tags which can be in Item description
ALLOWED_TAGS = ['b', 'blockquote', 'code', 'em', 'i', 'li', 'ol', 'strong', 'ul']

# Regex for Item ID with order (of image) validation
item_url_regular = re.compile(r"""
	^/
	(?P<item_id>([-_.:~a-zA-Z0-9]){1,255})
	/?
	(?P<order>\d*)
	""", re.VERBOSE)

# Regex for Item ID validation
id_regular = re.compile(r"""
	^([-_.:~a-zA-Z0-9]){1,255}$
	""", re.VERBOSE)

# Regex for general url validation
url_regular = re.compile(ur'(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:\'".,<>?\xab\xbb\u201c\u201d\u2018\u2019]))')


#@app.route('/')
def index():
	"""View function for index page"""
	
	return render_template('index.html')


#@app.route('/<item_id>')
#@app.route('/<item_id>/<order>')
def iFrame(item_id, order=None):
	"""View function for iFrame. Response with html page for zooming on item. If item has more images, particular image can be requested by order.
	'item_id' - ID of requested Item
	'order' - order of requested image in Item
	"""
	
	if order is not None:
		try:
			order = int(order)
			
			if order < 0:
				return 'Wrong item sequence', 404
		except:
			return 'Wrong item sequence', 404
	else:
		order = -1
	
	try:
		item = Item(item_id)
	except NoItemInDb as err:
		return err.message, 404
	except ErrorItemImport as err:
		return err.message, 500
	
	if item.lock is True:
		return 'The item is being ingested', 404
	
	if order >= len(item.url):
		return 'Wrong item sequence', 404
	
	tile_sources = []
	
	if order == -1:
		count = 0
		
		for url in item.url:
			tile_sources.append(prepareTileSources(item, url, count))
			count += 1
		
		order = 0
	else:
		url = item.url[order]
		tile_sources.append(prepareTileSources(item, url, order))
		
	return render_template('iframe_openseadragon_inline.html', item = item, tile_sources = tile_sources, order = order)


#@app.route('/<item_id>/manifest.json')
def iiifMeta(item_id):
	"""View function which returns IIIF manifest for particular Item
	'item_id' - ID of requested Item
	"""
	
	try:
		item = Item(item_id)
	except NoItemInDb as err:
		return err.message, 404
	except ErrorItemImport as err:
		return err.message, 500
	
	if item.lock is True:
		return 'The item is being ingested', 404
	
	fac = ManifestFactory()
	fac.set_base_metadata_uri(app.config['SERVER_NAME'])
	fac.set_base_metadata_dir(os.path.abspath(os.path.dirname(__file__)))
	fac.set_base_image_uri('http://%s' % app.config['IIIF_SERVER'])
	fac.set_iiif_image_info(2.0, 2)
	
	mf = fac.manifest(ident=url_for('iiifMeta', item_id=item_id, _external=True), label=item.title)
	mf.description = item.description
	mf.license = item.license
	
	mf.set_metadata({"label":"Author", "value":item.creator})
	mf.set_metadata({"label":"Source", "value":item.source})
	mf.set_metadata({"label":"Institution", "value":item.institution})
	mf.set_metadata({"label":"Institution link", "value":item.institution_link})
	
	seq = mf.sequence(ident='http://%s/sequence/s.json' % app.config['SERVER_NAME'], label='Item %s - sequence 1' % item_id)

	count = 0
	
	for url in item.url:
		if item.image_meta[url].has_key('width'):
			width = item.image_meta[url]['width']
		else:
			width = 1

		if item.image_meta[url].has_key('height'):
			height = item.image_meta[url]['height']
		else:
			height = 1
	
		cvs = seq.canvas(ident='http://%s/canvas/c%s.json' % (app.config['SERVER_NAME'], count), label='Item %s - image %s' % (item_id, count))
		cvs.set_hw(height, width)
	
		anno = cvs.annotation()
		
		if count == 0:
			filename = item_id
		else:
			filename = '%s/%s' % (item_id, count)

		img = anno.image(ident='/%s/full/full/0/native.jpg' % filename)
		img.add_service(ident='http://%s/%s' % (app.config['IIIF_SERVER'], filename), context='http://iiif.io/api/image/2/context.json', profile='http://iiif.io/api/image/2/profiles/level2.json')
		
		img.width = width
		img.height = height
		
		count += 1

	return json.JSONEncoder().encode(mf.toJSON(top=True)), 200, {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'}


#@app.route('/oembed', methods=['GET'])
def oEmbed():
	"""View function for oembed which returns medatada about Item which can be used to embed this Item to client page. Url is required parameter. Format (json or xml), maxwidth and maxheight are optional."""
	
	### Parameters configuration ###
	url = request.args.get('url', None)
	
	if url is None:
		return 'No url parameter provided', 404
	
	format = request.args.get('format', None)
	
	if format is None:
		format = 'json'
	
	if format not in ('json', 'xml'):
		return 'The format parameter must be "json" or "xml" (or blank)', 501
	
	p_url = urlparse(url)
	
	if p_url.scheme != 'http':
		return 'The http scheme must be used', 404
	
	if p_url.netloc != app.config['SERVER_NAME']:
		return 'Only urls on the same server are allowed', 404
	
	test = item_url_regular.search(p_url.path)
		
	if test:
		item_id = test.group('item_id')
		order = test.group('order')
		
		if order == '':
			order = 0
		else:
			order = int(order)
	else:
		return 'Unsupported format of ID', 404

	### Loading of Item from DB with testing ###
	try:
		item = Item(item_id)
	except NoItemInDb as err:
		return err.message, 404
	except ErrorItemImport as err:
		return err.message, 500
	
	if item.lock is True:
		return 'The item is being ingested', 404
		
	if order >= len(item.url):
		return 'Wrong item sequence', 404

	### Size of image configuration ###
	maxwidth = request.args.get('maxwidth', None)
	maxheight = request.args.get('maxheight', None)

	if maxwidth is not None:
		maxwidth = int(maxwidth)
	
	if maxheight is not None:
		maxheight = int(maxheight)

	if item.image_meta[item.url[order]].has_key('width'):
		width = int(item.image_meta[item.url[order]]['width'])
	else:
		width = -1

	if item.image_meta[item.url[order]].has_key('height'):
		height = int(item.image_meta[item.url[order]]['height'])
	else:
		height = -1
	
	if width != -1 and height != -1:
		ratio = float(width) / float(height)
	else:
		ratio = 1

	if width != -1:
		if maxwidth is not None and maxwidth < width:
			outwidth = maxwidth
		else:
			outwidth = 'full'
	else:
		if maxwidth is not None:
			outwidth = maxwidth
		else:
			outwidth = 'full'
	
	if height != -1:
		if maxheight is not None and maxheight < height:
			outheight = maxheight
		else:
			outheight = 'full'
	else:
		if maxheight is not None:
			outheight = maxheight
		else:
			outheight = 'full'
	
	if outwidth == 'full' and outheight == 'full':
		size = 'full'
	elif outwidth == 'full':
		size = ',%s' % outheight
		width = float(outheight) * ratio
		height =  outheight
	elif outheight == 'full':
		size = '%s,' % outwidth
		width = outwidth
		height = float(outwidth) / ratio
	else:
		size = '!%s,%s' % (outwidth, outheight)

		if ratio > (float(outwidth) / float(outheight)):
			width = outwidth
			height = float(outwidth) / ratio
		else:
			width = float(outheight) * ratio
			height = outheight
	
	### Output finalization ###
	if order == 0:
		filename = item_id
	else:
		filename = '%s/%s' % (item_id, order)
	
	data = {}
	data[u'version'] = '1.0'
	data[u'type'] = 'photo'
	data[u'title'] = item.title
	data[u'url'] = 'http://%s/%s/full/%s/0/native.jpg' % (app.config['IIIF_SERVER'], filename, size)
	data[u'width'] = '%.0f' % width
	data[u'height'] = '%.0f' % height
	data[u'author_name'] = item.creator
	data[u'author_url'] = item.source
	data[u'provider_name'] = item.institution
	data[u'provider_url'] = item.institution_link

	if format == 'xml':
		return render_template('oembed_xml.html', data = data), 200, {'Content-Type': 'text/xml'}
	else:
		return json.dumps(data), 200, {'Content-Type': 'application/json'}


#@app.route('/ingest', methods=['GET', 'POST'])
def ingest():
	"""View function for ingest. It takes json with items (by POST) to ingest or batch_id to show batch state."""
	
	### Show info about already started ingest (Batch) ###
	if request.method == 'GET':
		batch_id = request.args.get('batch_id', None)

		if batch_id is None:
			abort(404)
		
		try:
			batch = Batch(batch_id)
		except:
			abort(404)

		### Take info about completed tasks from Cloud Search ###
		try:
			cloudsearch = getCloudSearch(app.config['CLOUDSEARCH_BATCH_DOMAIN'], 'search')
			results = cloudsearch.search(q='batch_id:%s' % batch_id, parser='structured', size=500)
			finished_items = {}
			
			while results:
				for doc in results.docs:
					item_id = doc['fields']['item_id']
				
					if finished_items.has_key(item_id):
						finished_items[item_id].append(doc['fields']['status'])
					else:
						finished_items[item_id] = [doc['fields']['status']]
				
				results = results.next_page()
	
		except:
			return json.dumps({'errors': 'Problem with Cloud Search'}), 404, {'Content-Type': 'application/json'}

		output = []
		
		### Traverse through all items in batch, those items which are not stored on Cloud Search yet are pending ###
		for item_id in batch.items:
			tmp = {'id': item_id}
			
			if finished_items.has_key(item_id):
				if 'error' in finished_items[item_id]:
					tmp['status'] = 'error'
				elif 'pending' in finished_items[item_id]:
					tmp['status'] = 'pending'
				else:
					tmp['status'] = 'ok'
				
				try:
					if Task(batch_id, item_id, 0).item_tasks_count > len(finished_items[item_id]):
						tmp['status'] = 'pending'
				except:
					pass
				
			else:
				tmp['status'] = 'pending'
			
			output.append(tmp)
					
		return json.JSONEncoder().encode(output), 200, {'Content-Type': 'application/json'}
		
	### New ingest ###
	else:
		if request.headers.get('Content-Type') != 'application/json':
			abort(404)
		
		try:
			batch_data = json.loads(request.data)
		except:
			abort(404)

		if type(batch_data) is not list or len(batch_data) == 0:
			abort(404)
		
		if len(batch_data) > 10000:
			return json.dumps({'errors': 'maximum number of items is 10 000'}), 404, {'Content-Type': 'application/json'}
		
		item_ids = []
		errors = []
		
		### Validation ###
		for order in range(0, len(batch_data)):
			item = batch_data[order]
			
			if type(item) is not dict:
				errors.append("The item num. %s must be inside of '{}'" % order)
				continue
			
			item = dict((k.lower(), v) for k, v in item.iteritems())

			if not item.has_key('id'):
				errors.append("The item num. %s must have unique ID" % order)
				continue
			
			if item['id'] in item_ids:
				errors.append("The item num. %s must have unique ID" % order)
				continue
					
			if not id_regular.match(item['id']):
				errors.append("The item num. %s must have valid ID" % order)
			
			if item.has_key('status') and (len(item) != 2 or item['status'] != 'deleted'):
				errors.append("The item num. %s has status, but it isn't set to 'deleted' or there are more fields" % order)
				continue
			
			if item.has_key('status'):
				continue
			
			### Another tests are useful only for items which aren't marked to be deleted ###
			
			### Convert some input field's names to the internal names ###
			if item.has_key('institutionlink'):
				item['institution_link'] = item['institutionlink']
				item.pop('institutionlink', None)
			if item.has_key('imageurl'):
				item['url'] = item['imageurl']
				item.pop('imageurl', None)
			
			if not item.has_key('url') or type(item['url']) != list or len(item['url']) == 0:
				errors.append("The item num. %s doesn't have url field, or it isn't a list or a list is empty" % order)
				continue
			
			for url in item['url']:
				if not url_regular.match(url):
					errors.append("The '%s' url in the item num. %s isn't valid url" % (url, order))
			
			for key in item.keys():
				if key not in ['id', 'title', 'creator', 'source', 'institution', 'institution_link', 'license', 'description', 'url']:
					errors.append("The item num. %s has a not allowed field '%s'" % (order, key))
			
			if item.has_key('source') and item['source'] and not url_regular.match(item['source']):
				errors.append("The item num. %s doesn't have valid url '%s' in the Source field" % (order, item['source']))
			
			if item.has_key('institution_link') and item['institution_link'] and not url_regular.match(item['institution_link']):
				errors.append("The item num. %s doesn't have valid url '%s' in the InstitutionLink field" % (order, item['institution_link']))
			
			if item.has_key('license') and item['license'] and not url_regular.match(item['license']):
				errors.append("The item num. %s doesn't have valid url '%s' in the License field" % (order, item['license']))
			
			item_ids.append(item['id'])
			batch_data[order] = item
		
		if errors:
			return json.dumps({'errors': errors}), 404, {'Content-Type': 'application/json'}
			
		batch = Batch()

		### Storing of compressed json with all ingest orders to local disk ###
		f = gzip.open('/data/batch_%s.gz' % batch.id, 'wb')
		f.write(request.data)
		f.close()

		tasks = []
		
		### Processing items from ingest one by one ###
		for item_data in batch_data:
			item_id = item_data['id']
			
			batch.items.append(item_id)

			try:
				old_item = Item(item_id)
			except NoItemInDb, ErrorItemImport:
				old_item = None
			
			### Delete a item ###
			if item_data.has_key('status') and item_data['status'] == 'deleted':
				# if there is no item --> nothing is going to be done
				if old_item:
					task_order = 0
					
					for url in old_item.url:
						data = {'url': url, 'item_id': item_id, 'item_tasks_count': len(old_item.url), 'url_order': task_order, 'type': 'del'}
						task = Task(batch.id, item_id, task_order, data)
						tasks.append(task)
						task_order += 1
				else:
					continue
			
			### Update or create a new item ###
			else:
				### Sanitising input ###
				if item_data.has_key('title'):
					item_data['title'] = bleach.clean(item_data['title'], tags=[], attributes=[], styles=[], strip=True)
				if item_data.has_key('creator'):
					item_data['creator'] = bleach.clean(item_data['creator'], tags=[], attributes=[], styles=[], strip=True)
				if item_data.has_key('institution'):
					item_data['institution'] = bleach.clean(item_data['institution'], tags=[], attributes=[], styles=[], strip=True)
				if item_data.has_key('description'):
					item_data['description'] = bleach.clean(item_data['description'], tags=ALLOWED_TAGS, attributes=[], styles=[], strip=True)
					
				### Already stored item ###
				if old_item:
					new_count = len(item_data['url'])
					old_count = len(old_item.url)
					update_list = []
				
					for url_order in range(0, max(new_count, old_count)):
						if url_order < new_count and url_order < old_count:
							# different url on the specific position --> overwrite
							if item_data['url'][url_order] != old_item.url[url_order]:
								data = {'url': item_data['url'][url_order], 'item_id': item_id, 'url_order': url_order, 'type': 'add'}
								update_list.append(data)
						else:
							# end of both lists
							if new_count == old_count:
								break
						
							# a new url list is shorter than old one --> something to delelete
							if url_order >= new_count:
								data = {'url': old_item.url[url_order], 'item_id': item_id, 'url_order': url_order, 'type': 'del'}
								update_list.append(data)
							
							# a new url list is longer than old one --> something to add
							elif url_order >= old_count:
								data = {'url': item_data['url'][url_order], 'item_id': item_id, 'url_order': url_order, 'type': 'add'}
								update_list.append(data)
					
					### No change in url, change in other data possible ###
					if not update_list:
						data = {'item_id': item_id, 'type': 'mod', 'item_tasks_count': 1}
						task = Task(batch.id, item_id, 0, data)
						tasks.append(task)
					else:
						task_order = 0
						
						for data in update_list:
							data['item_tasks_count'] = len(update_list)
							task = Task(batch.id, item_id, task_order, data)
							tasks.append(task)
							task_order += 1
						
				### New item ###
				else:
					task_order = 0
				
					for url in item_data['url']:
						data = {'url': url, 'item_id': item_id, 'url_order': task_order, 'item_data': item_data, 'item_tasks_count': len(item_data['url']), 'type': 'add'}
						task = Task(batch.id, item_id, task_order, data)
						tasks.append(task)
						task_order += 1
					
			### Last task for specific item receives all item`s data ###
			task.item_data = item_data
			task.save()
		
			if old_item:
				old_item.lock = True
				old_item.save()
			
		batch.save()

		### Putting all tasks to the queue ###
		for task in tasks:
			ingestQueue.delay(batch.id, task.item_id, task.task_id)
		
	return json.JSONEncoder().encode({'batch_id': batch.id}), 200, {'Content-Type': 'application/json'}
