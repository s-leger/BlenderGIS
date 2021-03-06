import os
import time

import bpy
import bmesh
from bpy.types import Operator, Panel, AddonPreferences
from bpy.props import StringProperty, IntProperty, FloatProperty, BoolProperty, EnumProperty, FloatVectorProperty


from ..geoscene import GeoScene
from ..utils.geom import BBOX
from ..utils.proj import Reproj, reprojBbox, reprojPt
from ..utils.bpu import adjust3Dview
from ..utils import utm
from ..osm import overpy

from bpy_extras.view3d_utils import region_2d_to_location_3d, region_2d_to_vector_3d



# http://wiki.openstreetmap.org/wiki/Map_Features
osmkeys = [
	#'aerialway',
	#'aeroway',
	#'amenity',
	#'barrier',
	#'boundary',
	'building',
	#'craft',
	#'cycleway',
	#'emergency',
	#'geological',
	'highway',
	#'historic',
	'landuse',
	'leisure',
	#'man_made',
	#'military',
	'natural',
	#'office',
	#'places',
	#'power',
	#'public_transport',
	'railway',
	#'route'
	#'shop',
	#'sport',
	#'tourism',
	'waterway',
	'source=Bing'
]


closedWaysArePolygons = ['aeroway', 'amenity', 'boundary', 'building', 'craft', 'geological', 'historic', 'landuse', 'leisure', 'military', 'natural', 'office', 'place', 'shop' , 'sport', 'tourism']



def queryBuilder(bbox, tags=['building', 'highway'], types=['node', 'way', 'relation'], format='json'):

		'''
		QL template syntax :
		[out:json][bbox:ymin,xmin,ymax,xmax];(node[tag1];node[tag2];((way[tag1];way[tag2]);>);relation);out;
		'''

		#s,w,n,e <--> ymin,xmin,ymax,xmax
		bboxStr = ','.join(map(str, bbox.toLatlon()))

		if not types:
			#if no type filter is defined then just select all kind of type
			types = ['node', 'way', 'relation']

		head = "[out:"+format+"][bbox:"+bboxStr+"];"

		union = '('
		#all tagged nodes
		if 'node' in types:
			if tags:
				union += ';'.join( ['node['+tag+']' for tag in tags] ) + ';'
			else:
				union += 'node;'
		#all tagged ways with all their nodes (recurse down)
		if 'way' in types:
			union += '(('
			if tags:
				union += ';'.join( ['way['+tag+']' for tag in tags] ) + ');'
			else:
				union += 'way);'
			union += '>);'
		#all relations (no filter tag applied)
		if 'relation' in types or 'rel' in types:
			union += 'relation'
		union += ')'

		output = ';out;'
		qry = head + union + output

		return qry





########################
def joinBmesh(src_bm, dest_bm):

	buff = bpy.data.meshes.new(".temp")
	src_bm.to_mesh(buff)

	dest_bm.from_mesh(buff)

	bpy.data.meshes.remove(buff)




class OSM_IMPORT():
	"""Import from Open Street Map"""

	def EnumTags(self, context):
		tags = []
		for tag in osmkeys:
			#put each item in a tuple (key, label, tooltip)
			tags.append( (tag, tag, tag) )
		return tags

	filterTags = EnumProperty(
			name="Tags",
			description="Select tags to include",
			items = EnumTags,
			options = {"ENUM_FLAG"})

	featureType = EnumProperty(
			name="Type",
			description="Select types to include",
			items = [
				('node', 'Nodes', 'Request all nodes'),
				('way', 'Ways', 'Request all ways'),
				('relation', 'Relations', 'Request all relations')
			],
			default = {'way'},
			options = {"ENUM_FLAG"}
			)

	separate = BoolProperty(name='Separate objects', description='Warning : can be very slow with lot of features')


	def draw(self, context):
		layout = self.layout
		row = layout.row()
		row.prop(self, "featureType", expand=True)
		row = layout.row()
		col = row.column()
		col.prop(self, "filterTags", expand=True)
		layout.prop(self, 'separate')



	def build(self, context, result, dstCRS):
		scn = context.scene
		geoscn = GeoScene(scn)
		scale = geoscn.scale #TODO

		#Init reprojector class
		try:
			rprj = Reproj(4326, dstCRS)
		except Exception as e:
			self.report({'ERROR'}, "Unable to reproject data. " + str(e))
			return {'FINISHED'}


		bmeshes = {}
		vgroupsObj = {}

		#######
		def seed(id, tags, pts):
			'''
			Sub funtion :
				1. create a bmesh from [pts]
				2. seed meshesData array or create a new object
			'''
			if len(pts) > 1:
				if pts[0] == pts[-1] and any(tag in closedWaysArePolygons for tag in tags):
					type = 'Areas'
					closed = True
					pts.pop() #exclude last duplicate node
				else:
					type = 'Ways'
					closed = False
			else:
				type = 'Nodes'
				closed = False

			#reproj and shift coords
			pts = rprj.pts(pts)
			dx, dy = geoscn.crsx, geoscn.crsy
			pts = [ (v[0]-dx, v[1]-dy, 0) for v in pts]

			#Create a new bmesh
			#>using an intermediate bmesh object allows some extra operation like extrusion
			bm = bmesh.new()

			if len(pts) == 1:
				verts = [bm.verts.new(pt) for pt in pts]

			elif closed:
				verts = [bm.verts.new(pt) for pt in pts]
				face = bm.faces.new(verts)
				#ensure face is up (anticlockwise order)
				#because in OSM there is no particular order for closed ways
				face.normal_update()
				if face.normal.z < 0:
					face.normal_flip()

			elif len(pts) > 1: #edge
				#Split polyline to lines
				n = len(pts)
				lines = [ (pts[i], pts[i+1]) for i in range(n) if i < n-1 ]
				for line in lines:
					verts = [bm.verts.new(pt) for pt in line]
					edge = bm.edges.new(verts)



			if self.separate:

				#Clean up and update the bmesh
				#bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)


				name = tags.get('name', str(id))

				mesh = bpy.data.meshes.new(name)
				bm.to_mesh(mesh)
				mesh.update()
				mesh.validate()

				obj = bpy.data.objects.new(name, mesh)

				#Assign tags
				obj['id'] = str(id) #cast to str to avoid overflow error "Python int too large to convert to C int"
				for key in tags.keys():
					obj[key] = tags[key]

				scn.objects.link(obj)
				obj.select = True


			else:
				#Grouping
				#extract bmesh data to python list formated as required by from_pydata function
				#using from_pydata is the fastest way to produce a large mesh (appending lot of geom to the same bmesh is exponentially slow)


				bm.verts.index_update()
				#bm.edges.index_update()
				#bm.faces.index_update()


				if self.filterTags:

					#group by tags (there could be some duplicates)
					for k in self.filterTags:

						if k in extags: #
							objName = type + ':' + k
							kbm = bmeshes.setdefault(objName, bmesh.new())
							offset = len(kbm.verts)
							joinBmesh(bm, kbm)

				else:
					#group all into one unique mesh
					objName = type
					_bm = bmeshes.setdefault(objName, bmesh.new())
					offset = len(_bm.verts)
					joinBmesh(bm, _bm)


				#vertex group
				name = tags.get('name', None)
				vidx = [v.index + offset for v in bm.verts]
				vgroups = vgroupsObj.setdefault(objName, {})

				for tag in extags:
					#if tag in osmkeys:#filter
					if not tag.startswith('name'):
						vgroup = vgroups.setdefault('Tag:'+tag, [])
						vgroup.extend(vidx)

				if name is not None:
					#vgroup['Name:'+name] = [vidx]
					vgroup = vgroups.setdefault('Name:'+name, [])
					vgroup.extend(vidx)

				if 'relation' in self.featureType:
					for rel in result.relations:
						name = rel.tags.get('name', str(rel.id))
						for member in rel.members:
							#todo: remove duplicate members
							if id == member.ref:
								vgroup = vgroups.setdefault('Relation:'+name, [])
								vgroup.extend(vidx)




			bm.free()


		#

		#Build mesh
		waysNodesId = [node.id for way in result.ways for node in way.nodes]

		if 'node' in self.featureType:

			for node in result.nodes:

				#extended tags list
				extags = list(node.tags.keys()) + [k + '=' + v for k, v in node.tags.items()]

				if node.id in waysNodesId:
					continue

				if self.filterTags and not any(tag in self.filterTags for tag in extags):
					continue

				pt = (float(node.lon), float(node.lat))
				seed(node.id, node.tags, [pt])


		if 'way' in self.featureType:


			for way in result.ways:

				extags = list(way.tags.keys()) + [k + '=' + v for k, v in way.tags.items()]

				if self.filterTags and not any(tag in self.filterTags for tag in extags):
					continue

				#if way.nodes[0].id == way.nodes[-1].id:
				#	closed = True
				#else:
				#	closed = False

				pts = [(float(node.lon), float(node.lat)) for node in way.nodes]
				#if closed:
				#	pts.pop() #exclude last duplicate node
				seed(way.id, way.tags, pts)



		if not self.separate:
			'''
			#Clean up and update the bmesh
			bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
			bm.verts.index_update()
			bm.edges.index_update()
			bm.faces.index_update()
			'''

			for name, bm in bmeshes.items():
				mesh = bpy.data.meshes.new(name)
				bm.to_mesh(mesh)
				bm.free()


				mesh.update()#calc_edges=True)
				mesh.validate()
				obj = bpy.data.objects.new(name, mesh)
				scn.objects.link(obj)
				obj.select = True

				vgroups = vgroupsObj.get(name, None)
				if vgroups is not None:
					#for vgroupName, vgroupIdx in vgroups.items():
					for vgroupName in sorted(vgroups.keys()):
						vgroupIdx = vgroups[vgroupName]
						g = obj.vertex_groups.new(vgroupName)
						g.add(vgroupIdx, weight=1, type='ADD')




		if 'relation' in self.featureType and self.separate:

			groups = bpy.data.groups
			objects = scn.objects

			for rel in result.relations:

				name = rel.tags.get('name', str(rel.id))

				for member in rel.members:

					#todo: remove duplicate members

					g = groups.get(name, groups.new(name))

					for obj in objects:
						#id = int(obj.get('id', -1))
						try:
							id = int(obj['id'])
						except:
							id = None
						if id == member.ref:
							try:
								g.objects.link(obj)
							except Exception as e:
								#print('Unable to put ' + obj.name + ' in ' + name)
								#print(str(e)) #error already in group
								pass









#######################

class OSM_FILE(Operator, OSM_IMPORT):

	bl_idname = "importgis.osm_file"
	bl_description = 'Select and import osm xml file'
	bl_label = "Import OSM"
	bl_options = {"UNDO"}

	# Import dialog properties
	filepath = StringProperty(
		name="File Path",
		description="Filepath used for importing the file",
		maxlen=1024,
		subtype='FILE_PATH' )

	filename_ext = ".osm"

	filter_glob = StringProperty(
			default = "*.osm",
			options = {'HIDDEN'} )

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def execute(self, context):

		scn = context.scene

		if not os.path.exists(self.filepath):
			self.report({'ERROR'}, "Invalid file")
			return{'FINISHED'}

		try:
			bpy.ops.object.mode_set(mode='OBJECT')
		except:
			pass
		bpy.ops.object.select_all(action='DESELECT')

		#Set cursor representation to 'loading' icon
		w = context.window
		w.cursor_set('WAIT')

		#Spatial ref system
		geoscn = GeoScene(scn)
		if geoscn.isBroken:
				self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
				return {'FINISHED'}

		#Parse file
		t0 = time.clock()
		api = overpy.Overpass()
		#with open(self.filepath, "r", encoding"utf-8") as f:
		#	result = api.parse_xml(f.read()) #WARNING read() load all the file into memory
		result = api.parse_xml(self.filepath)
		t = time.clock() - t0
		print('parsed in %f' % t)

		#Get bbox
		bounds = result.bounds
		lon = (bounds["minlon"] + bounds["maxlon"])/2
		lat = (bounds["minlat"] + bounds["maxlat"])/2
		#Set CRS
		if not geoscn.hasCRS:
			try:
				geoscn.crs = utm.lonlat_to_epsg(lon, lat)
			except Exception as e:
				self.report({'ERROR'}, str(e))
				return {'FINISHED'}
		#Set scene origin georef
		if not geoscn.hasOriginPrj:
			x, y = reprojPt(4326, geoscn.crs, lon, lat)
			geoscn.setOriginPrj(x, y)


		#Build meshes
		t0 = time.clock()
		self.build(context, result, geoscn.crs)
		t = time.clock() - t0
		print('build in %f' % t)

		bbox = BBOX.fromScn(scn)
		adjust3Dview(context, bbox)

		return{'FINISHED'}




########################

class OSM_QUERY(Operator, OSM_IMPORT):
	"""Import from Open Street Map"""

	bl_idname = "importgis.osm_query"
	bl_description = 'Import through an overpass query, OSM data which cover view3d area'
	bl_label = "Import OSM"
	bl_options = {"UNDO"}



	def invoke(self, context, event):

		#check if 3dview is top ortho
		reg3d = context.region_data
		if reg3d.view_perspective != 'ORTHO' or tuple(reg3d.view_matrix.to_euler()) != (0,0,0):
			self.report({'ERROR'}, "View3d must be in top ortho")
			return {'FINISHED'}

		#check georef
		geoscn = GeoScene(context.scene)
		if not geoscn.isGeoref:
				self.report({'ERROR'}, "Scene is not georef")
				return {'FINISHED'}
		if geoscn.isBroken:
				self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
				return {'FINISHED'}

		return context.window_manager.invoke_props_dialog(self)



	def execute(self, context):

		scn = context.scene
		geoscn = GeoScene(scn)

		try:
			bpy.ops.object.mode_set(mode='OBJECT')
		except:
			pass
		bpy.ops.object.select_all(action='DESELECT')

		#Set cursor representation to 'loading' icon
		w = context.window
		w.cursor_set('WAIT')

		#Get view3d bbox in lonlat
		bbox = BBOX.fromTopView(context).toGeo(geoscn)
		if bbox.dimensions.x > 20000 or bbox.dimensions.y > 20000:
			self.report({'ERROR'}, "Too large extent")
			return {'FINISHED'}
		bbox = reprojBbox(geoscn.crs, 4326, bbox)

		#Download from overpass api
		api = overpy.Overpass()

		query = queryBuilder(bbox, tags=list(self.filterTags), types=list(self.featureType), format='xml')

		print(query)
		try:
			result = api.query(query)
		except Exception as e:
			print(str(e))
			self.report({'ERROR'}, "Overpass query failed")
			return {'FINISHED'}
		else:
			print('Overpass query success')

		self.build(context, result, geoscn.crs)

		bbox = BBOX.fromScn(scn)
		adjust3Dview(context, bbox, zoomToSelect=False)

		return {'FINISHED'}





class OSM_PANEL(Panel):
	bl_category = "GIS"
	bl_label = "Get OSM"
	bl_space_type = "VIEW_3D"
	bl_context = "objectmode"
	bl_region_type = "TOOLS"#"UI"


	def draw(self, context):
		layout = self.layout
		layout.operator("importgis.osm_query")
		'''
		scn = context.scene
		addonPrefs = context.user_preferences.addons[PKG].preferences
		row = layout.row(align=True)
		row.operator("view3d.map_start")
		row.operator("bgis.pref_show", icon='SCRIPTWIN', text='')
		'''
