// The reusable field editors of the configuration UI.
//
//   ScalarInput        text / number / yes-no / choice
//   OptionSelect       dropdown over { value, label }
//   Combobox           text field with a searchable suggestion list
//   EntityPicker       Home Assistant entity, searchable by name, id and area
//   EntityValueInput   an option or attribute of an entity chosen elsewhere
//   SecretPicker       reference to (and creation of) a secret in secrets.yaml
//   ListEditor         ordered list of simple values
//   TableEditor        list of records, one column per field
//   MapEditor          mapping with free keys
//   JsonEditor         raw fallback
//   LoadProfileEditor  static load profile per day type
//   PluginEditor       type + that plugin's settings (tariff, forecast)
//   LearningEditor     on / off / on with options
//   FieldInput         picks the editor for a spec
//   SchemaField        label + FieldInput + hint
//   SchemaForm         grid of SchemaFields for a mapping
//
// They nest in each other (a form holds a table whose cells are fields …), so
// they are registered globally instead of importing one another.
import ScalarInput from './ScalarInput.vue'
import OptionSelect from './OptionSelect.vue'
import Combobox from './Combobox.vue'
import EntityPicker from './EntityPicker.vue'
import EntityValueInput from './EntityValueInput.vue'
import SecretPicker from './SecretPicker.vue'
import ListEditor from './ListEditor.vue'
import TableEditor from './TableEditor.vue'
import MapEditor from './MapEditor.vue'
import JsonEditor from './JsonEditor.vue'
import LoadProfileEditor from './LoadProfileEditor.vue'
import PluginEditor from './PluginEditor.vue'
import LearningEditor from './LearningEditor.vue'
import FieldInput from './FieldInput.vue'
import SchemaField from './SchemaField.vue'
import SchemaForm from './SchemaForm.vue'

const COMPONENTS = {
  ScalarInput, OptionSelect, Combobox, EntityPicker, EntityValueInput, SecretPicker, ListEditor, TableEditor, MapEditor,
  JsonEditor, LoadProfileEditor, PluginEditor, LearningEditor, FieldInput, SchemaField, SchemaForm,
}

export function registerFieldComponents(app) {
  for (const [name, component] of Object.entries(COMPONENTS)) app.component(name, component)
}
