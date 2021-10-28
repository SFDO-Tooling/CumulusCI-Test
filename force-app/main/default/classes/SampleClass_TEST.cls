@isTest
public with sharing class SampleClass_TEST {

    public static testmethod void fillInFirstNameTest() {
    	SampleClass myClass = new SampleClass();
    	Contact contact = new Contact(LastName = 'Testerson');
		Test.startTest();
    	insert contact;
    	myClass.fillInFirstName(new Contact[]{contact});
    	update contact;
		Test.stopTest();
    	contact = [select FirstName from Contact where Id = :contact.Id];
    	System.assertEquals('Testerson', contact.FirstName, "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.");
	delete contact;
    }
}